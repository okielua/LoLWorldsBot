"""Background source refresh and public reminder checks."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging

from models import MatchStatus
from services.reminder_engine import ReminderTracker, SERVER_RECOVERY_WINDOW_SECONDS
from services.reminder_scheduler import ReminderCheckResult, check_reminders


logger = logging.getLogger(__name__)
REMINDER_INTERVAL_SECONDS = 15
MAX_REMINDER_DATA_AGE = timedelta(minutes=5)


class BackgroundPolling:
    def __init__(self, store, load_users, publish_data, wait_until_ready, *, clock=None, server_delivery=None, voting_manager=None):
        self.store = store
        self.load_users = load_users
        self.publish_data = publish_data
        self.wait_until_ready = wait_until_ready
        self.clock = clock or (lambda: datetime.now(UTC))
        self.server_delivery = server_delivery
        self.voting_manager = voting_manager
        # Preview state is deliberately separate from any future delivery ledger.
        self.preview_tracker = ReminderTracker()
        self.failures = 0
        self._tasks = {}
        self._extra_tasks = set()
        self.prediction_test_running = False
        self._paused = False
        self._prediction_refresh = asyncio.Event()

    def start(self):
        jobs = [
            ("source-refresh", self._refresh_loop),
            ("reminder-preview", self._preview_loop),
        ]
        if self.voting_manager:
            jobs.append(("pregame-polls", self._poll_loop))
        for name, coroutine in jobs:
            task = self._tasks.get(name)
            if task is None or task.done():
                self._tasks[name] = asyncio.create_task(coroutine(), name=name)
        logger.info(
            "Background polling started; public delivery %s",
            "managed by /serversetup" if self.server_delivery else "disabled",
        )

    async def stop(self):
        tasks = list(self._tasks.values()) + list(self._extra_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._extra_tasks.clear()
        if self.server_delivery:
            await self.server_delivery.flush_pending_records()
        flush = getattr(self.voting_manager, "flush_pending_records", None)
        if flush:
            await flush()

    def manage_test_task(self, task):
        self._extra_tasks.add(task)
        task.add_done_callback(self._extra_tasks.discard)

    def refresh_interval_seconds(self):
        now = self.clock()
        active = any(
            match.status == MatchStatus.LIVE
            or (
                match.status == MatchStatus.SCHEDULED
                and -timedelta(hours=1) <= match.start_time - now <= timedelta(hours=1)
            )
            for match in self.store.data.matches
        )
        delay = 60 if active else 300
        if self.failures:
            delay = min(60 * 2 ** min(self.failures - 1, 4), 60 if active else 900)
        retry_not_before = getattr(self.store, "retry_not_before", None)
        if retry_not_before is not None:
            delay = max(delay, (retry_not_before - now).total_seconds())
        return delay

    async def refresh_once(self):
        old_matches = self.store.data.matches_by_id
        try:
            success = await self.store.refresh()
        except Exception as error:
            # A programming/source error must not silently kill the recurring task.
            self.store.last_error = str(error)
            logger.exception("Unexpected source refresh failure")
            success = False
        if not success:
            self.failures += 1
            return False
        if self.failures:
            logger.info("Live source recovered after %s failed refreshes", self.failures)
        self.failures = 0
        new_matches = self.store.data.matches_by_id
        for match_id, old in old_matches.items():
            new = new_matches.get(match_id)
            if new is None or (
                old.start_time, old.status, old.team1_id, old.team2_id
            ) != (new.start_time, new.status, new.team1_id, new.team2_id):
                self.preview_tracker.forget_match(match_id)
        self.publish_data(self.store.data)
        self._prediction_refresh.set()
        return True

    def _can_preview(self, now):
        age = now - self.store.fetched_at if self.store.fetched_at else None
        usable = (
            not self.store.last_error
            and age is not None
            and timedelta(0) <= age <= MAX_REMINDER_DATA_AGE
        )
        if not usable:
            if not self._paused:
                logger.warning("Reminder checks paused: live data unavailable or stale")
            self._paused = True
            return False
        if self._paused:
            logger.info("Reminder checks resumed with fresh source data")
        self._paused = False
        return True

    async def preview_once(self):
        if self.server_delivery:
            await self.server_delivery.flush_pending_records()
        if not self._can_preview(self.clock()):
            return ReminderCheckResult((), ())
        users = await asyncio.to_thread(self.load_users)
        # Use the current snapshot after the database read, not a saved startup list.
        now = self.clock()
        if not self._can_preview(now):
            return ReminderCheckResult((), ())
        result = check_reminders(
            self.store.data.matches, users, now, self.preview_tracker, include_dm=False,
            server_window_seconds=SERVER_RECOVERY_WINDOW_SECONDS,
        )
        if self.server_delivery:
            candidates = check_reminders(
                self.store.data.matches, users, now, self.server_delivery.tracker,
                mark_sent=False, include_dm=False,
                server_window_seconds=SERVER_RECOVERY_WINDOW_SECONDS,
            )
            for reminder in candidates.server_reminders:
                await self.server_delivery.deliver(reminder.match_id)
        else:
            for reminder in result.server_reminders:
                logger.info(
                    "DRY RUN: server reminder due for %s (%s ping recipients); nothing sent",
                    reminder.match_id, len(reminder.ping_user_ids),
                )
        return result

    async def _refresh_loop(self):
        await self.wait_until_ready()
        # setup_hook already made the first fetch. Retry soon if it failed.
        if self.store.last_error:
            self.failures = 1
        while True:
            await asyncio.sleep(self.refresh_interval_seconds())
            try:
                await self.refresh_once()
            except Exception:
                self.failures += 1
                logger.exception("Source polling iteration failed; will retry")

    async def _preview_loop(self):
        await self.wait_until_ready()
        while True:
            try:
                await self.preview_once()
            except Exception:
                logger.exception("Reminder preview iteration failed; will retry")
            await asyncio.sleep(REMINDER_INTERVAL_SECONDS)

    async def _poll_loop(self):
        await self.wait_until_ready()
        while True:
            self._prediction_refresh.clear()
            try:
                await self.voting_manager.tick()
            except Exception:
                logger.exception("Pregame poll iteration failed; will retry")
            try:
                await asyncio.wait_for(self._prediction_refresh.wait(), timeout=REMINDER_INTERVAL_SECONDS)
            except TimeoutError:
                pass
