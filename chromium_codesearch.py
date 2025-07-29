import json
import random
import string
import time
from typing import Dict
from urllib.request import Request, urlopen

ALPHANUMERIC = string.ascii_letters + string.digits

API_KEY = "AIzaSyCqPSptx9mClE5NU4cpfzr6cgdO_phV1lM"
URL_ENDPOINT = f"https://grimoireoss-pa.clients6.google.com/batch"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "content-type": "text/plain; charset=UTF-8",
    "origin": "https://source.chromium.org",
    "pragma": "no-cache",
    "referer": "https://source.chromium.org/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
}


def make_body(endpoint: str, payload: Dict):
    boundary = f"batch{int(time.time() * 1000)}{str(random.random())[2:]}"
    body = "\r\n".join(
        [
            f"--{boundary}",
            "Content-Type: application/http",
            f"Content-ID: <response-{boundary}+gapiRequest@googleapis.com>",
            "",
            f"POST {endpoint}?alt=json&key={API_KEY}",
            f"sessionid: {''.join(random.choice(ALPHANUMERIC) for i in range(10))}",
            f"actionid: {''.join(random.choice(ALPHANUMERIC) for i in range(10))}",
            "X-JavaScript-User-Agent: google-api-javascript-client/1.1.0",
            "X-Requested-With: XMLHttpRequest",
            "Content-Type: application/json",
            "X-Goog-Encode-Response-If-Executable: base64",
            "",
            json.dumps(payload),
            f"--{boundary}--",
            "",
        ]
    )

    return [boundary, body]


# Based on https://github.com/hjanuschka/chromium-helper/blob/f3db5a551429b7f9d8c3c0fcc0f75f61a335da34/src/index.ts#L3410
def search(query: str, page_size: int = 25):
    payload = {
        "queryString": query,
        "searchOptions": {
            "enableDiagnostics": False,
            "exhaustive": False,
            "numberOfContextLines": 1,
            "pageSize": page_size,
            "pageToken": "",
            "pathPrefix": "",
            "repositoryScope": {"root": {"ossProject": "chromium", "repositoryName": "chromium/src"}},
            "retrieveMultibranchResults": True,
            "savedQuery": "",
            "scoringModel": "",
            "showPersonalizedResults": False,
            "suppressGitLegacyResults": False,
        },
        "snippetOptions": {"minSnippetLinesPerFile": 10, "minSnippetLinesPerPage": 60, "numberOfContextLines": 1},
    }

    boundary, body = make_body("/v1/contents/search", payload)
    request = Request(
        f"{URL_ENDPOINT}?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}",
        data=body.encode(),
        method="POST",
        headers=HEADERS,
    )

    with urlopen(request) as response:
        payload = response.read()
        start_idx = payload.index(b"\r\n\r\n{")
        end_idx = payload[start_idx:].index(b"--batch")

        return json.loads(payload[start_idx : start_idx + end_idx].strip())["searchResults"]


# Example response:
#
#  [
#    {
#      "title": "ComponentRegistration",
#      "symbol": {
#        "type": "STRUCT",
#        "range": {
#          "length": 21
#        }
#      },
#      "fileSpec": {
#        "sourceRoot": {
#          "repositoryKey": {
#            "repositoryName": "chromium/src",
#            "ossProject": "chromium"
#          },
#          "refSpec": "refs/heads/main"
#        },
#        "path": "components/component_updater/component_updater_service.h"
#      },
#      "lineNumber": 82,
#      "resultToken": "CAASAiABGAMiIDY3MjllNDk2NmEwYzAwNDI1ZjEyNTM3Yjc4N2M3NzYwKFE4gIH73eq_u6q4AUAASAA",
#      "match": {
#        "lineNumber": 82,
#        "lineText": "struct ComponentRegistration {\n",
#        "range": {
#          "startIndex": 7,
#          "length": 21
#        }
#      }
#    }
#  ]
def suggest(query: str):
    payload = {
        "queryString": query,
        "suggestOptions": {
            "enableDiagnostics": False,
            "maxSuggestions": 7,
            "pathPrefix": "",
            "repositoryScope": {},
            "retrieveMultibranchResults": True,
            "savedQuery": "",
            "showPersonalizedResults": False,
            "suppressGitLegacyResults": False,
        },
    }

    boundary, body = make_body("/v1/contents/suggest", payload)
    request = Request(
        f"{URL_ENDPOINT}?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}",
        data=body.encode(),
        method="POST",
        headers=HEADERS,
    )

    with urlopen(request) as response:
        payload = response.read()
        start_idx = payload.index(b"\r\n\r\n{")
        end_idx = payload[start_idx:].index(b"--batch")

        return json.loads(payload[start_idx : start_idx + end_idx].strip())["suggestions"]
