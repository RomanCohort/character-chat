"""Entry point: python -m character_chat"""
import sys
import pathlib

# Ensure correct path dependencies when run as module
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from character_chat.cli import Cli


def main():
    import argparse
    from loguru import logger

    # First list available characters (need config loaded)
    available = Cli.list_characters()
    parser = argparse.ArgumentParser(description="Character Chat Terminal")
    parser.add_argument(
        "-c", "--character",
        choices=available if available else None,
        default=available[0] if available else None,
        help="Character name (default: first available)"
    )
    parser.add_argument(
        "--neural",
        action="store_true",
        help="Enable neural emotion classifier (loads HuggingFace model, slower)"
    )
    args = parser.parse_args()

    if not available:
        logger.error("No character loaded. Check config/characters/*.yaml")
        sys.exit(1)

    name = args.character if args.character else available[0]
    cli = Cli(character_name=name, neural_enabled=args.neural)
    cli.run()


if __name__ == "__main__":
    main()
