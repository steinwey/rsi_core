"""Explicit public vocabulary setup; never invoked by the router or model client."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

from rsi.skills.tokenizers import CACHE_PATH, VOCAB_SHA256, VOCAB_URL


def install(source=None):
    if CACHE_PATH.exists():
        if hashlib.sha256(CACHE_PATH.read_bytes()).hexdigest() != VOCAB_SHA256:
            raise ValueError('Existing vocabulary is corrupt; not overwriting it.')
        return CACHE_PATH
    if source is not None:
        data = Path(source).read_bytes()
    else:
        # Public static asset, no API credentials, inference or token-counting requests.
        with urllib.request.urlopen(VOCAB_URL, timeout=60) as response:
            data = response.read(8 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != VOCAB_SHA256:
        raise ValueError('Vocabulary SHA256 mismatch; nothing installed.')
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves an existing file, including concurrent setup attempts.
    try:
        with CACHE_PATH.open('xb') as stream:
            stream.write(data)
    except FileExistsError:
        if hashlib.sha256(CACHE_PATH.read_bytes()).hexdigest() != VOCAB_SHA256:
            raise ValueError('Concurrent vocabulary installation did not match.')
    return CACHE_PATH


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-file', help='Import a verified offline copy instead of downloading')
    args = parser.parse_args()
    print(install(args.from_file))
    print('Verified SHA256: ' + VOCAB_SHA256)


if __name__ == '__main__':
    main()
