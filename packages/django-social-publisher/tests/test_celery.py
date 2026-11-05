from dataclasses import asdict

import httpx
import pytest
import respx

pytest.importorskip("celery")

from social_publisher.adapters.youtube import UPLOAD_URL
from social_publisher.contrib.celery.tasks import publish_task, reconcile_task
from tests.helpers import SESSION_URI, fixture, make_account, media_spec, metadata

pytestmark = pytest.mark.django_db


@respx.mock
def test_tasks_wrap_services_with_serialisable_arguments():
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": SESSION_URI}))
    respx.put(SESSION_URI).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": "bytes=0-262143"}),
            httpx.Response(201, json=fixture("video_inserted")),
        ]
    )
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(200, json=fixture("videos_list_processed"))
    )
    account = make_account()
    publication_id = publish_task(
        str(account.pk), asdict(media_spec()), asdict(metadata()), "key-1"
    )
    assert reconcile_task(publication_id) == "published"
