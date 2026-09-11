"""Run a paper-trading session.

    python -m apps.orchestrator                 # offline synthetic market
    TF_FEED=live python -m apps.orchestrator    # public exchange feeds

Serves the dashboard and API alongside the trading loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from apps.api.app import create_app
from apps.orchestrator.wiring import build_platform
from core.config import load_settings
from core.logging import configure_logging
from core.models.runtime import ShutdownStage

log = logging.getLogger("apps.orchestrator")


def _build_server(platform, host: str, port: int):
    import uvicorn

    class _EmbeddedServer(uvicorn.Server):
        """Uvicorn embedded under the trading-floor process lifecycle.

        Uvicorn is normally the top-level process and therefore owns SIGTERM
        and SIGINT. Here it is one task beside the orchestrator and LUMEN, so
        the trading-floor process must own those signals and coordinate all
        tasks through the same shutdown path. Both hooks are provided for
        compatibility across the supported Uvicorn range: older releases use
        ``install_signal_handlers`` while newer releases use
        ``capture_signals``.
        """

        def install_signal_handlers(self) -> None:
            return None

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    config = uvicorn.Config(
        create_app(platform), host=host, port=port, log_level="warning", access_log=False
    )
    return _EmbeddedServer(config)


def _process_shutdown_signals(stop: asyncio.Event):
    """Make the trading-floor process, not embedded Uvicorn, own termination.

    Docker stops the container with SIGTERM. The previous process shape let
    Uvicorn consume that signal for the API task only while the orchestrator
    and LUMEN loops kept running inside ``asyncio.gather``. Docker eventually
    reached its kill timeout, so ``Platform.stop()`` and ``Recorder.stop()``
    never ran and the durable session stayed OPEN.

    The handler does one thing: request the shared application stop. Cleanup
    remains in ``run()`` and therefore keeps the existing ordered Phase 11
    lifecycle and recorder-integrity semantics.
    """

    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(signum, _frame) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - OS supplied an unknown signal
            signal_name = str(signum)
        log.info("shutdown requested", extra={"signal": signal_name})
        loop.call_soon_threadsafe(stop.set)

    @contextlib.contextmanager
    def installed():
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    return installed()


async def _serve_api(server) -> None:
    """Serve until asked to stop.

    The server is shut down through its own ``should_exit`` flag rather than by
    cancelling the task, so its lifespan handler unwinds cleanly instead of
    raising a CancelledError traceback on every shutdown.
    """
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format, service="trading-floor")

    platform = build_platform(settings, session_label=args.label)
    log.info(
        "starting paper session",
        extra={
            # The three axes, stated rather than left to be inferred. ``mode``
            # is PAPER in every case; ``profile`` says what the session is
            # for; ``feed`` says where prices come from. A SHADOW profile is
            # still paper execution against the same paper account.
            "mode": settings.mode.value,
            "profile": settings.operational_profile.value,
            "feed": settings.feed.value,
            "executor": "PaperExecutor",
            "session_id": platform.session_id,
            "venues": [v.name for v in settings.enabled_venues],
            "symbols": settings.symbols,
            "initial_balance": settings.paper_initial_balance,
            "intelligence_provider": platform.lumen.provider.name,
        },
    )

    stop = asyncio.Event()
    timer_handle = None
    tasks: list[asyncio.Task] = []
    stop_waiter = None
    server = None

    with _process_shutdown_signals(stop):
        await platform.start_bus(record=not args.no_record, feeds=True)
        await platform.start(record=not args.no_record, feeds=True)

        tasks = [
            asyncio.create_task(platform.orchestrator.run_forever(), name="orchestrator"),
            asyncio.create_task(platform.lumen.run_forever(), name="lumen"),
        ]
        if not args.no_api:
            server = _build_server(platform, settings.api_host, settings.api_port)
            tasks.append(asyncio.create_task(_serve_api(server), name="api"))
            log.info(
                "dashboard available",
                extra={"url": f"http://{settings.api_host}:{settings.api_port}/"},
            )

        if args.duration:
            timer_handle = asyncio.get_running_loop().call_later(args.duration, stop.set)

        stop_waiter = asyncio.create_task(stop.wait(), name="shutdown-request")

        try:
            done, _ = await asyncio.wait(
                [*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED
            )

            # A runtime task ending on its own is not a reason to leave the
            # other forever-tasks running. Propagate a real failure; otherwise
            # turn the unexpected clean exit into the same coordinated stop.
            if stop_waiter not in done:
                for task in done:
                    if task is stop_waiter or task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                    log.warning(
                        "runtime task exited; requesting coordinated shutdown",
                        extra={"task": task.get_name()},
                    )
                stop.set()
        except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
            stop.set()
        finally:
            if timer_handle is not None:
                timer_handle.cancel()
            if stop_waiter is not None:
                stop_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_waiter

            # Phase 11 witnesses the real process shutdown order. No signal
            # handler performs cleanup itself: every exit path converges here.
            platform.operations.mark_stopping(platform.clock.now_ms())
            api_task = next((t for t in tasks if t.get_name() == "api"), None)
            if server is not None and api_task is not None:
                platform.operations.set_shutdown_stage(
                    ShutdownStage.STOPPING_API, platform.clock.now_ms()
                )
                # Let uvicorn unwind its own lifespan before anything is
                # cancelled, otherwise every shutdown prints a traceback.
                server.should_exit = True
                with contextlib.suppress(TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(api_task), timeout=5.0)

            platform.operations.set_shutdown_stage(
                ShutdownStage.STOPPING_LOOPS, platform.clock.now_ms()
            )
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            await platform.stop()
            log.info(
                "session ended",
                extra={
                    "session_id": platform.session_id,
                    "profile": settings.operational_profile.value,
                    "feed": settings.feed.value,
                    "ticks": platform.orchestrator.ticks,
                    "events_recorded": platform.recorder.events_recorded,
                    "net_pnl": (
                        platform.state.portfolio.net_pnl
                        if platform.state.portfolio
                        else 0.0
                    ),
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a paper-trading session")
    parser.add_argument("--label", default="", help="label recorded with the session")
    parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    parser.add_argument("--no-api", action="store_true", help="do not serve the dashboard")
    parser.add_argument("--no-record", action="store_true", help="do not persist events")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
