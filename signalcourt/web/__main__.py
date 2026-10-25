"""Start the console on the local machine only."""

import uvicorn

from . import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8765, log_level="warning", access_log=False)
