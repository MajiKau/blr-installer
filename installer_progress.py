def show_progress(stage: str, current: int, total: int | None) -> None:
    if total is not None:
        fraction = 1 if total == 0 else min(current / total, 1)
        width = 30
        bar = "#" * int(width * fraction) + "-" * (width - int(width * fraction))
        message = (
            f"\r{stage}: [{bar}] {fraction:>3.0%} "
            f"({_format_size(current)} / {_format_size(total)})"
        )
    else:
        message = f"\r{stage}: {_format_size(current)} transferred"
    print(message, end="", flush=True)


def finish_progress() -> None:
    print(flush=True)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
