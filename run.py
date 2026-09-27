import uvicorn

from video_uploader.config import load_config

if __name__ == "__main__":
    config = load_config()
    uvicorn.run("video_uploader.web.app:app", host=config.server.host, port=config.server.port)
