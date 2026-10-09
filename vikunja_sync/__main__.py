from dotenv import load_dotenv

from vikunja_sync.config import load_settings
from vikunja_sync.sync import run


def main() -> None:
    load_dotenv()
    run(load_settings())


if __name__ == "__main__":
    main()
