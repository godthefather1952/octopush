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

from apps.api.app import create_app
from apps.orchestrator.wiring import build_platform
from core.config import load_settings
from core.logging import configure_logging
from core.models.runtime import ShutdownStage

log = logging.getLogger("apps.orchestrator")


def _build_server(platform, host: str, port: int):
    import uvicorn

    config = uvicorn.Config(
        create_app(platform), host=host, port=port, log_level="warning", access_log=False
    )
    return uvicorn.Server(config)


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

    await platform.start(record=not args.no_record)

    tasks = [
        asyncio.create_task(platform.orchestrator.run_forever(), name="orchestrator"),
        asyncio.create_task(platform.lumen.run_forever(), name="lumen"),
    ]
    server = None
    if not args.no_api:
        server = _build_server(platform, settings.api_host, settings.api_port)
        tasks.append(asyncio.create_task(_serve_api(server), name="api"))
        log.info(
            "dashboard available",
            extra={"url": f"http://{settings.api_host}:{settings.api_port}/"},
        )

    stop = asyncio.Event()
    if args.duration:
        async def _timer() -> None:
            await asyncio.sleep(args.duration)
            stop.set()

        tasks.append(asyncio.create_task(_timer(), name="duration"))

    try:
        if args.duration:
            await stop.wait()
        else:
            await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        pass
    finally:
        # Phase 11 witnesses the same shutdown order the process has always
        # used. These observations do not control or suppress shutdown.
        platform.operations.mark_stopping(platform.clock.now_ms())
        api_task = next((t for t in tasks if t.get_name() == "api"), None)
        if server is not None and api_task is not None:
            platform.operations.set_shutdown_stage(
                ShutdownStage.STOPPING_API, platform.clock.now_ms()
            )
            # Let uvicorn unwind its own lifespan before anything is cancelled,
            # otherwise every shutdown prints a CancelledError traceback.
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
                    platform.state.portfolio.net_pnl if platform.state.portfolio else 0.0
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
