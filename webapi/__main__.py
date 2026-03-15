import os

import uvicorn


def main() -> None:
    host = os.getenv("HERMES_WEBAPI_HOST", "127.0.0.1")
    port = int(os.getenv("HERMES_WEBAPI_PORT", "8642"))
    uvicorn.run("webapi.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
