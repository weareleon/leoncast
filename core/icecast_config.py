"""
leonCAST - Icecast Config Generator
Generates an icecast.xml with one <mount> block per station, derived
from the stations table. Run this after adding/removing stations, then
reload/restart Icecast.
"""

from pathlib import Path
from data import db

TEMPLATE_HEAD = """<icecast>
    <location>leonCAST</location>
    <admin>admin@leoncast.local</admin>

    <limits>
        <clients>200</clients>
        <sources>{num_sources}</sources>
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-on-connect>1</burst-on-connect>
        <burst-size>65535</burst-size>
    </limits>

    <authentication>
        <source-password>{default_source_password}</source-password>
        <relay-password>hackme</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>{admin_password}</admin-password>
    </authentication>

    <hostname>{hostname}</hostname>

    <listen-socket>
        <port>{port}</port>
    </listen-socket>

    <fileserve>1</fileserve>

    <paths>
        <basedir>/usr/share/icecast</basedir>
        <logdir>/var/log/icecast</logdir>
        <webroot>/usr/share/icecast/web</webroot>
        <adminroot>/usr/share/icecast/admin</adminroot>
        <alias source="/" destination="/status.xsl"/>
    </paths>

    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>3</loglevel>
        <logsize>10000</logsize>
    </logging>

    <security>
        <chroot>0</chroot>
    </security>

"""

TEMPLATE_MOUNT = """    <mount type="normal">
        <mount-name>{mount}</mount-name>
        <username>source</username>
        <password>{source_password}</password>
        <max-listeners>500</max-listeners>
        <fallback-mount>/silence.mp3</fallback-mount>
        <fallback-override>1</fallback-override>
        <public>0</public>
    </mount>

"""

TEMPLATE_TAIL = "</icecast>\n"


def generate_icecast_config(hostname: str = "localhost", port: int = 8000,
                             admin_password: str = "changeme",
                             default_source_password: str = "changeme") -> str:
    stations = db.list_stations()

    out = TEMPLATE_HEAD.format(
        num_sources=max(len(stations), 4),
        default_source_password=default_source_password,
        admin_password=admin_password,
        hostname=hostname,
        port=port,
    )

    for s in stations:
        out += TEMPLATE_MOUNT.format(
            mount=s["icecast_mount"],
            source_password=s["icecast_source_password"],
        )

    out += TEMPLATE_TAIL
    return out


def write_icecast_config(path: str | None = None, **kwargs) -> str:
    if path is None:
        # Was previously hardcoded to a path from the dev sandbox this project
        # was built in -- broke on every other machine. Write next to the DB.
        path = str(Path(__file__).parent.parent / "data" / "icecast.xml")
    content = generate_icecast_config(**kwargs)
    Path(path).write_text(content)
    return path


if __name__ == "__main__":
    out_path = write_icecast_config()
    print(f"Wrote {out_path}")
