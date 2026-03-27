#!/usr/bin/env python3
# pylint: disable=no-member,method-hidden

import os
import sys
import asyncio
import base64
import hashlib
from pathlib import Path
from aiohttp import web
from aiohttp.log import access_logger
import ssl
import socket
import socketio
import logging
import json
import pathlib
import re
from watchfiles import DefaultFilter, Change, awatch

from ytdl import DownloadQueueNotifier, DownloadQueue
from yt_dlp.version import __version__ as yt_dlp_version

log = logging.getLogger("main")


def parseLogLevel(logLevel):
    match logLevel:
        case "DEBUG":
            return logging.DEBUG
        case "INFO":
            return logging.INFO
        case "WARNING":
            return logging.WARNING
        case "ERROR":
            return logging.ERROR
        case "CRITICAL":
            return logging.CRITICAL
        case _:
            return None


# Configure logging before Config() uses it so early messages are not dropped.
# Only configure if no handlers are set (avoid clobbering hosting app settings).
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=parseLogLevel(os.environ.get("LOGLEVEL", "INFO")) or logging.INFO
    )


class Config:
    _DEFAULTS = {
        "DOWNLOAD_DIR": ".",
        "AUDIO_DOWNLOAD_DIR": "%%DOWNLOAD_DIR",
        "TEMP_DIR": "%%DOWNLOAD_DIR",
        "DOWNLOAD_DIRS_INDEXABLE": "false",
        "CUSTOM_DIRS": "true",
        "CREATE_CUSTOM_DIRS": "true",
        "CUSTOM_DIRS_EXCLUDE_REGEX": r"(^|/)[.@].*$",
        "DELETE_FILE_ON_TRASHCAN": "false",
        "STATE_DIR": ".",
        "URL_PREFIX": "",
        "PUBLIC_HOST_URL": "download/",
        "PUBLIC_HOST_AUDIO_URL": "audio_download/",
        "OUTPUT_TEMPLATE": "%(title)s.%(ext)s",
        "OUTPUT_TEMPLATE_CHAPTER": "%(title)s - %(section_number)02d - %(section_title)s.%(ext)s",
        "OUTPUT_TEMPLATE_PLAYLIST": "%(playlist_title)s/%(title)s.%(ext)s",
        "OUTPUT_TEMPLATE_CHANNEL": "%(channel)s/%(title)s.%(ext)s",
        "DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT": "0",
        "YTDL_OPTIONS": "{}",
        "YTDL_OPTIONS_FILE": "",
        "ROBOTS_TXT": "",
        "HOST": "0.0.0.0",
        "PORT": "8081",
        "HTTPS": "false",
        "CERTFILE": "",
        "KEYFILE": "",
        "BASE_DIR": "",
        "DEFAULT_THEME": "auto",
        "MAX_CONCURRENT_DOWNLOADS": 3,
        "LOGLEVEL": "INFO",
        "ENABLE_ACCESSLOG": "false",
    }

    _BOOLEAN = (
        "DOWNLOAD_DIRS_INDEXABLE",
        "CUSTOM_DIRS",
        "CREATE_CUSTOM_DIRS",
        "DELETE_FILE_ON_TRASHCAN",
        "HTTPS",
        "ENABLE_ACCESSLOG",
    )

    def __init__(self):
        for k, v in self._DEFAULTS.items():
            setattr(self, k, os.environ.get(k, v))

        for k, v in self.__dict__.items():
            if isinstance(v, str) and v.startswith("%%"):
                setattr(self, k, getattr(self, v[2:]))
            if k in self._BOOLEAN:
                if v not in ("true", "false", "True", "False", "on", "off", "1", "0"):
                    log.error(
                        f'Environment variable "{k}" is set to a non-boolean value "{v}"'
                    )
                    sys.exit(1)
                setattr(self, k, v in ("true", "True", "on", "1"))

        if not self.URL_PREFIX.endswith("/"):
            self.URL_PREFIX += "/"

        # Convert relative addresses to absolute addresses to prevent the failure of file address comparison
        if self.YTDL_OPTIONS_FILE and self.YTDL_OPTIONS_FILE.startswith("."):
            self.YTDL_OPTIONS_FILE = str(Path(self.YTDL_OPTIONS_FILE).resolve())

        success, _ = self.load_ytdl_options()
        if not success:
            sys.exit(1)

    def load_ytdl_options(self) -> tuple[bool, str]:
        try:
            self.YTDL_OPTIONS = json.loads(os.environ.get("YTDL_OPTIONS", "{}"))
            assert isinstance(self.YTDL_OPTIONS, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = "Environment variable YTDL_OPTIONS is invalid"
            log.error(msg)
            return (False, msg)

        if not self.YTDL_OPTIONS_FILE:
            return (True, "")

        log.info(f'Loading yt-dlp custom options from "{self.YTDL_OPTIONS_FILE}"')
        if not os.path.exists(self.YTDL_OPTIONS_FILE):
            msg = f'File "{self.YTDL_OPTIONS_FILE}" not found'
            log.error(msg)
            return (False, msg)
        try:
            with open(self.YTDL_OPTIONS_FILE) as json_data:
                opts = json.load(json_data)
            assert isinstance(opts, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = "YTDL_OPTIONS_FILE contents is invalid"
            log.error(msg)
            return (False, msg)

        self.YTDL_OPTIONS.update(opts)
        return (True, "")


config = Config()
# Align root logger level with Config (keeps a single source of truth).
# This re-applies the log level after Config loads, in case LOGLEVEL was
# overridden by config file settings or differs from the environment variable.
logging.getLogger().setLevel(parseLogLevel(str(config.LOGLEVEL)) or logging.INFO)


class ObjectSerializer(json.JSONEncoder):
    def default(self, obj):
        # First try to use __dict__ for custom objects
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        # Convert iterables (generators, dict_items, etc.) to lists
        # Exclude strings and bytes which are also iterable
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            try:
                return list(obj)
            except:
                pass
        # Fall back to default behavior
        return json.JSONEncoder.default(self, obj)


serializer = ObjectSerializer()
app = web.Application()
sio = socketio.AsyncServer(cors_allowed_origins="*")
sio.attach(app, socketio_path=config.URL_PREFIX + "socket.io")
routes = web.RouteTableDef()


DEFAULT_USER = os.environ.get("DEFAULT_USER", "local")


def _decode_cf_email(token):
    """Extract email from a Cloudflare Access JWT (payload only, no sig check —
    Cloudflare already validated it at the edge)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("email")
    except Exception:
        return None


def _get_cf_token_from_environ(environ):
    """Extract CF Access JWT from a Socket.IO environ dict (aiohttp)."""
    # aiohttp request object
    request = environ.get("aiohttp.request")
    if request and hasattr(request, "headers"):
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if token:
            return token
    # WSGI-style
    token = environ.get("HTTP_CF_ACCESS_JWT_ASSERTION")
    if token:
        return token
    # ASGI-style raw headers
    headers = environ.get("headers", [])
    if isinstance(headers, (list, tuple)):
        for name, value in headers:
            name_str = name.decode() if isinstance(name, bytes) else name
            if name_str.lower() == "cf-access-jwt-assertion":
                return value.decode() if isinstance(value, bytes) else value
    return None


def get_user_from_request(request):
    """Get user email from an aiohttp request."""
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if token:
        email = _decode_cf_email(token)
        if email:
            return email.lower()
    return DEFAULT_USER


def get_user_from_environ(environ):
    """Get user email from a Socket.IO connect environ."""
    token = _get_cf_token_from_environ(environ)
    if token:
        email = _decode_cf_email(token)
        if email:
            return email.lower()
    return DEFAULT_USER


def user_state_dir(email):
    """Return (and create) a per-user state directory."""
    user_hash = hashlib.sha256(email.encode()).hexdigest()[:16]
    path = os.path.join(config.STATE_DIR, user_hash)
    os.makedirs(path, exist_ok=True)
    return path


# sid -> user email
user_sessions = {}


class UserNotifier(DownloadQueueNotifier):
    """Emits Socket.IO events scoped to a single user's room."""

    def __init__(self, user_email):
        self.room = f"user:{user_email}"

    async def added(self, dl):
        log.info(f"Notifier[{self.room}]: Download added - {dl.title}")
        await sio.emit("added", serializer.encode(dl), room=self.room)

    async def updated(self, dl):
        log.debug(f"Notifier[{self.room}]: Download updated - {dl.title}")
        await sio.emit("updated", serializer.encode(dl), room=self.room)

    async def completed(self, dl):
        log.info(f"Notifier[{self.room}]: Download completed - {dl.title}")
        await sio.emit("completed", serializer.encode(dl), room=self.room)

    async def canceled(self, id):
        log.info(f"Notifier[{self.room}]: Download canceled - {id}")
        await sio.emit("canceled", serializer.encode(id), room=self.room)

    async def cleared(self, id):
        log.info(f"Notifier[{self.room}]: Download cleared - {id}")
        await sio.emit("cleared", serializer.encode(id), room=self.room)


class UserQueueManager:
    """Lazily creates and caches per-user DownloadQueue instances."""

    def __init__(self, config):
        self.config = config
        self._queues = {}

    async def get_queue(self, user_email):
        if user_email not in self._queues:
            sd = user_state_dir(user_email)
            notifier = UserNotifier(user_email)
            queue = DownloadQueue(self.config, notifier, state_dir=sd)
            await queue.initialize()
            self._queues[user_email] = queue
            log.info(f"Created queue for user {user_email} (state: {sd})")
        return self._queues[user_email]


queue_manager = UserQueueManager(config)


class FileOpsFilter(DefaultFilter):
    def __call__(self, change_type: int, path: str) -> bool:
        # Check if this path matches our YTDL_OPTIONS_FILE
        if path != config.YTDL_OPTIONS_FILE:
            return False

        # For existing files, use samefile comparison to handle symlinks correctly
        if os.path.exists(config.YTDL_OPTIONS_FILE):
            try:
                if not os.path.samefile(path, config.YTDL_OPTIONS_FILE):
                    return False
            except (OSError, IOError):
                # If samefile fails, fall back to string comparison
                if path != config.YTDL_OPTIONS_FILE:
                    return False

        # Accept all change types for our file: modified, added, deleted
        return change_type in (Change.modified, Change.added, Change.deleted)


def get_options_update_time(success=True, msg=""):
    result = {"success": success, "msg": msg, "update_time": None}

    # Only try to get file modification time if YTDL_OPTIONS_FILE is set and file exists
    if config.YTDL_OPTIONS_FILE and os.path.exists(config.YTDL_OPTIONS_FILE):
        try:
            result["update_time"] = os.path.getmtime(config.YTDL_OPTIONS_FILE)
        except (OSError, IOError) as e:
            log.warning(
                f"Could not get modification time for {config.YTDL_OPTIONS_FILE}: {e}"
            )
            result["update_time"] = None

    return result


async def watch_files():
    async def _watch_files():
        async for changes in awatch(
            config.YTDL_OPTIONS_FILE, watch_filter=FileOpsFilter()
        ):
            success, msg = config.load_ytdl_options()
            result = get_options_update_time(success, msg)
            await sio.emit("ytdl_options_changed", serializer.encode(result))

    log.info(f"Starting Watch File: {config.YTDL_OPTIONS_FILE}")
    asyncio.create_task(_watch_files())


if config.YTDL_OPTIONS_FILE:
    app.on_startup.append(lambda app: watch_files())


@routes.post(config.URL_PREFIX + "add")
async def add(request):
    user = get_user_from_request(request)
    dqueue = await queue_manager.get_queue(user)
    log.info(f"[{user}] Received request to add download")
    post = await request.json()
    log.info(f"[{user}] Request data: {post}")
    url = post.get("url")
    quality = post.get("quality")
    if not url or not quality:
        log.error("Bad request: missing 'url' or 'quality'")
        raise web.HTTPBadRequest()
    format = post.get("format")
    folder = post.get("folder")
    custom_name_prefix = post.get("custom_name_prefix")
    playlist_item_limit = post.get("playlist_item_limit")
    auto_start = post.get("auto_start")
    split_by_chapters = post.get("split_by_chapters")
    chapter_template = post.get("chapter_template")

    if custom_name_prefix is None:
        custom_name_prefix = ""
    if auto_start is None:
        auto_start = True
    if playlist_item_limit is None:
        playlist_item_limit = config.DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT
    if split_by_chapters is None:
        split_by_chapters = False
    if chapter_template is None:
        chapter_template = config.OUTPUT_TEMPLATE_CHAPTER

    playlist_item_limit = int(playlist_item_limit)

    status = await dqueue.add(
        url,
        quality,
        format,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start,
        split_by_chapters,
        chapter_template,
    )
    return web.Response(text=serializer.encode(status))


@routes.post(config.URL_PREFIX + "delete")
async def delete(request):
    user = get_user_from_request(request)
    dqueue = await queue_manager.get_queue(user)
    post = await request.json()
    ids = post.get("ids")
    where = post.get("where")
    if not ids or where not in ["queue", "done"]:
        log.error("Bad request: missing 'ids' or incorrect 'where' value")
        raise web.HTTPBadRequest()
    status = await (dqueue.cancel(ids) if where == "queue" else dqueue.clear(ids))
    log.info(
        f"[{user}] Download delete request processed for ids: {ids}, where: {where}"
    )
    return web.Response(text=serializer.encode(status))


@routes.post(config.URL_PREFIX + "start")
async def start(request):
    user = get_user_from_request(request)
    dqueue = await queue_manager.get_queue(user)
    post = await request.json()
    ids = post.get("ids")
    log.info(f"[{user}] Received request to start pending downloads for ids: {ids}")
    status = await dqueue.start_pending(ids)
    return web.Response(text=serializer.encode(status))


@routes.get(config.URL_PREFIX + "history")
async def history(request):
    user = get_user_from_request(request)
    dqueue = await queue_manager.get_queue(user)
    history = {"done": [], "queue": [], "pending": []}

    for _, v in dqueue.queue.saved_items():
        history["queue"].append(v)
    for _, v in dqueue.done.saved_items():
        history["done"].append(v)
    for _, v in dqueue.pending.saved_items():
        history["pending"].append(v)

    log.info(f"[{user}] Sending download history")
    return web.Response(text=serializer.encode(history))


@sio.event
async def connect(sid, environ):
    user = get_user_from_environ(environ)
    user_sessions[sid] = user
    sio.enter_room(sid, f"user:{user}")
    log.info(f"Client connected: {sid} (user: {user})")

    dqueue = await queue_manager.get_queue(user)
    await sio.emit("all", serializer.encode(dqueue.get()), to=sid)
    await sio.emit("configuration", serializer.encode(config), to=sid)
    if config.CUSTOM_DIRS:
        await sio.emit("custom_dirs", serializer.encode(get_custom_dirs()), to=sid)
    if config.YTDL_OPTIONS_FILE:
        await sio.emit(
            "ytdl_options_changed", serializer.encode(get_options_update_time()), to=sid
        )


@sio.event
async def disconnect(sid):
    user = user_sessions.pop(sid, None)
    log.info(f"Client disconnected: {sid} (user: {user})")


def get_custom_dirs():
    def recursive_dirs(base):
        path = pathlib.Path(base)

        # Converts PosixPath object to string, and remove base/ prefix
        def convert(p):
            s = str(p)
            if s.startswith(base):
                s = s[len(base) :]

            if s.startswith("/"):
                s = s[1:]

            return s

        # Include only directories which do not match the exclude filter
        def include_dir(d):
            if len(config.CUSTOM_DIRS_EXCLUDE_REGEX) == 0:
                return True
            else:
                return re.search(config.CUSTOM_DIRS_EXCLUDE_REGEX, d) is None

        # Recursively lists all subdirectories of DOWNLOAD_DIR
        dirs = list(filter(include_dir, map(convert, path.glob("**/"))))

        return dirs

    download_dir = recursive_dirs(config.DOWNLOAD_DIR)

    audio_download_dir = download_dir
    if config.DOWNLOAD_DIR != config.AUDIO_DOWNLOAD_DIR:
        audio_download_dir = recursive_dirs(config.AUDIO_DOWNLOAD_DIR)

    return {"download_dir": download_dir, "audio_download_dir": audio_download_dir}


@routes.get(config.URL_PREFIX)
def index(request):
    response = web.FileResponse(
        os.path.join(config.BASE_DIR, "ui/dist/metube/browser/index.html")
    )
    if "metube_theme" not in request.cookies:
        response.set_cookie("metube_theme", config.DEFAULT_THEME)
    return response


@routes.get(config.URL_PREFIX + "robots.txt")
def robots(request):
    if config.ROBOTS_TXT:
        response = web.FileResponse(os.path.join(config.BASE_DIR, config.ROBOTS_TXT))
    else:
        response = web.Response(
            text="User-agent: *\nDisallow: /download/\nDisallow: /audio_download/\n"
        )
    return response


@routes.get(config.URL_PREFIX + "version")
def version(request):
    return web.json_response(
        {"yt-dlp": yt_dlp_version, "version": os.getenv("METUBE_VERSION", "dev")}
    )


if config.URL_PREFIX != "/":

    @routes.get("/")
    def index_redirect_root(request):
        return web.HTTPFound(config.URL_PREFIX)

    @routes.get(config.URL_PREFIX[:-1])
    def index_redirect_dir(request):
        return web.HTTPFound(config.URL_PREFIX)


routes.static(
    config.URL_PREFIX + "download/",
    config.DOWNLOAD_DIR,
    show_index=config.DOWNLOAD_DIRS_INDEXABLE,
)
routes.static(
    config.URL_PREFIX + "audio_download/",
    config.AUDIO_DOWNLOAD_DIR,
    show_index=config.DOWNLOAD_DIRS_INDEXABLE,
)
routes.static(
    config.URL_PREFIX, os.path.join(config.BASE_DIR, "ui/dist/metube/browser")
)
try:
    app.add_routes(routes)
except ValueError as e:
    if "ui/dist/metube/browser" in str(e):
        raise RuntimeError(
            "Could not find the frontend UI static assets. Please run `node_modules/.bin/ng build` inside the ui folder"
        ) from e
    raise e


# https://github.com/aio-libs/aiohttp/pull/4615 waiting for release
# @routes.options(config.URL_PREFIX + 'add')
async def add_cors(request):
    return web.Response(text=serializer.encode({"status": "ok"}))


app.router.add_route("OPTIONS", config.URL_PREFIX + "add", add_cors)


async def on_prepare(request, response):
    if "Origin" in request.headers:
        response.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"


app.on_response_prepare.append(on_prepare)


def supports_reuse_port():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.close()
        return True
    except (AttributeError, OSError):
        return False


def isAccessLogEnabled():
    if config.ENABLE_ACCESSLOG:
        return access_logger
    else:
        return None


if __name__ == "__main__":
    logging.getLogger().setLevel(parseLogLevel(config.LOGLEVEL) or logging.INFO)
    log.info(f"Listening on {config.HOST}:{config.PORT}")

    if config.HTTPS:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=config.CERTFILE, keyfile=config.KEYFILE)
        web.run_app(
            app,
            host=config.HOST,
            port=int(config.PORT),
            reuse_port=supports_reuse_port(),
            ssl_context=ssl_context,
            access_log=isAccessLogEnabled(),
        )
    else:
        web.run_app(
            app,
            host=config.HOST,
            port=int(config.PORT),
            reuse_port=supports_reuse_port(),
            access_log=isAccessLogEnabled(),
        )
