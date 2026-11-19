#GOAL Download official SEC insider archives with an identified and rate-limited request.

import os
from pathlib import Path
import re
from threading import Lock
from time import monotonic, sleep

import httpx

from .sec_form345_input import LoadError, SOURCE_CODE


_SEC_QUARTERLY_ZIP = re.compile(
    r"https://www\.sec\.gov/files/(?:structureddata/data|datastandardsinnovation/data|dera/data|data)/insider-transactions-data-sets/"
    r"[0-9]{4}q[1-4]_form345\.zip"
)
_CONTACT_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_REQUEST_INTERVAL = 1 / 8
_REQUEST_LOCK = Lock()
_NEXT_REQUEST_AT = 0.0


def _live_options(url: str) -> str:
    if not _SEC_QUARTERLY_ZIP.fullmatch(url):
        raise LoadError(
            artifact_uri=None, reason="--live requires an official www.sec.gov quarterly Forms 3/4/5 ZIP URL"
        )
    agent = os.environ.get("SEC_USER_AGENT", "")
    organization, _, contact = agent.strip().rpartition(" ")
    if (
        not organization.strip()
        or not _CONTACT_EMAIL.fullmatch(contact)
        or not all(" " <= character <= "~" for character in agent)
    ):
        raise LoadError(artifact_uri=url, reason="Set SEC_USER_AGENT to an organization name and contact email")
    return agent.strip()


def _download(url: str, user_agent: str, destination: Path) -> None:
    global _NEXT_REQUEST_AT

    with httpx.Client(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(60, connect=10)) as client:
        with _REQUEST_LOCK:
            # A one-token bucket refills 125 ms after the preceding attempt.
            # Hold the lock through the attempt to prevent request bursts.
            while (wait := _NEXT_REQUEST_AT - monotonic()) > 0:
                sleep(wait)
            try:
                with client.stream(
                    "GET", url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code != 200:
                        raise LoadError(artifact_uri=url, reason=f"SEC returned HTTP {response.status_code}")
                    with destination.open("wb") as output:
                        for chunk in response.iter_raw():
                            output.write(chunk)
            except httpx.RequestError as error:
                raise LoadError(artifact_uri=url, reason=f"SEC request failed ({type(error).__name__})") from error
            finally:
                _NEXT_REQUEST_AT = monotonic() + _REQUEST_INTERVAL


def main(argv=None):
    from mim.cli import legacy_load

    return legacy_load(SOURCE_CODE, argv)
