"""The tests for hls streams."""
import asyncio
import itertools
import re
from urllib.parse import urlparse

import pytest

from homeassistant.components.stream import create_stream
from homeassistant.components.stream.const import (
    ATTR_SETTINGS,
    CONF_LL_HLS,
    CONF_PART_DURATION,
    CONF_SEGMENT_DURATION,
    DOMAIN,
    HLS_PROVIDER,
)
from homeassistant.components.stream.core import Part
from homeassistant.const import HTTP_NOT_FOUND
from homeassistant.setup import async_setup_component

from .test_hls import SEGMENT_DURATION, STREAM_SOURCE, HlsClient, make_playlist

from tests.components.stream.common import (
    FAKE_TIME,
    DefaultSegment as Segment,
    generate_h264_video,
)

TEST_PART_DURATION = 1
NUM_PART_SEGMENTS = int(-(-SEGMENT_DURATION // TEST_PART_DURATION))
PART_INDEPENDENT_PERIOD = int(1 / TEST_PART_DURATION) or 1
BYTERANGE_LENGTH = 1
INIT_BYTES = b"init"
SEQUENCE_BYTES = bytearray(range(NUM_PART_SEGMENTS * BYTERANGE_LENGTH))
ALT_SEQUENCE_BYTES = bytearray(range(20, 20 + NUM_PART_SEGMENTS * BYTERANGE_LENGTH))
VERY_LARGE_LAST_BYTE_POS = 9007199254740991


@pytest.fixture
def hls_stream(hass, hass_client):
    """Create test fixture for creating an HLS client for a stream."""

    async def create_client_for_stream(stream):
        stream.ll_hls = True
        http_client = await hass_client()
        parsed_url = urlparse(stream.endpoint_url(HLS_PROVIDER))
        return HlsClient(http_client, parsed_url)

    return create_client_for_stream


def create_segment(sequence):
    """Create an empty segment."""
    segment = Segment(sequence=sequence)
    segment.init = INIT_BYTES
    return segment


def complete_segment(segment):
    """Completes a segment by setting its duration."""
    segment.duration = sum(
        part.duration for part in segment.parts_by_byterange.values()
    )


def create_parts(source):
    """Create parts from a source."""
    independent_cycle = itertools.cycle(
        [True] + [False] * (PART_INDEPENDENT_PERIOD - 1)
    )
    return [
        Part(
            duration=TEST_PART_DURATION,
            has_keyframe=next(independent_cycle),
            data=bytes(source[i * BYTERANGE_LENGTH : (i + 1) * BYTERANGE_LENGTH]),
        )
        for i in range(NUM_PART_SEGMENTS)
    ]


def http_range_from_part(part):
    """Return dummy byterange (length, start) given part number."""
    return BYTERANGE_LENGTH, part * BYTERANGE_LENGTH


def make_segment_with_parts(
    segment, num_parts, independent_period, discontinuity=False
):
    """Create a playlist response for a segment including part segments."""
    response = []
    for i in range(num_parts):
        length, start = http_range_from_part(i)
        response.append(
            f'#EXT-X-PART:DURATION={TEST_PART_DURATION:.3f},URI="./segment/{segment}.m4s",BYTERANGE="{length}@{start}"{",INDEPENDENT=YES" if i%independent_period==0 else ""}'
        )
    if discontinuity:
        response.append("#EXT-X-DISCONTINUITY")
    response.extend(
        [
            "#EXT-X-PROGRAM-DATE-TIME:"
            + FAKE_TIME.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            f"#EXTINF:{SEGMENT_DURATION:.3f},",
            f"./segment/{segment}.m4s",
        ]
    )
    return "\n".join(response)


def make_hint(segment, part):
    """Create a playlist response for the preload hint."""
    _, start = http_range_from_part(part)
    return f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="./segment/{segment}.m4s",BYTERANGE-START={start}'


async def test_ll_hls_stream(hass, hls_stream, stream_worker_sync):
    """
    Test hls stream.

    Purposefully not mocking anything here to test full
    integration with the stream component.
    """
    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream_worker_sync.pause()

    # Setup demo HLS track
    source = generate_h264_video(duration=SEGMENT_DURATION + 1)
    stream = create_stream(hass, source, {})

    # Request stream
    stream.add_provider(HLS_PROVIDER)
    stream.start()

    hls_client = await hls_stream(stream)

    # Fetch playlist
    master_playlist_response = await hls_client.get()
    assert master_playlist_response.status == 200

    # Fetch init
    master_playlist = await master_playlist_response.text()
    init_response = await hls_client.get("/init.mp4")
    assert init_response.status == 200

    # Fetch playlist
    playlist_url = "/" + master_playlist.splitlines()[-1]
    playlist_response = await hls_client.get(playlist_url)
    assert playlist_response.status == 200

    # Fetch segments
    playlist = await playlist_response.text()
    segment_re = re.compile(r"^(?P<segment_url>./segment/\d+\.m4s)")
    for line in playlist.splitlines():
        match = segment_re.match(line)
        if match:
            segment_url = "/" + match.group("segment_url")
            segment_response = await hls_client.get(segment_url)
            assert segment_response.status == 200

    def check_part_is_moof_mdat(data: bytes):
        if len(data) < 8 or data[4:8] != b"moof":
            return False
        moof_length = int.from_bytes(data[0:4], byteorder="big")
        if (
            len(data) < moof_length + 8
            or data[moof_length + 4 : moof_length + 8] != b"mdat"
        ):
            return False
        mdat_length = int.from_bytes(
            data[moof_length : moof_length + 4], byteorder="big"
        )
        if mdat_length + moof_length != len(data):
            return False
        return True

    # Fetch all completed part segments
    part_re = re.compile(
        r'#EXT-X-PART:DURATION=[0-9].[0-9]{5,5},URI="(?P<part_url>.+?)",BYTERANGE="(?P<byterange_length>[0-9]+?)@(?P<byterange_start>[0-9]+?)"(,INDEPENDENT=YES)?'
    )
    for line in playlist.splitlines():
        match = part_re.match(line)
        if match:
            part_segment_url = "/" + match.group("part_url")
            byterange_end = (
                int(match.group("byterange_length"))
                + int(match.group("byterange_start"))
                - 1
            )
            part_segment_response = await hls_client.get(
                part_segment_url,
                headers={
                    "Range": f'bytes={match.group("byterange_start")}-{byterange_end}'
                },
            )
            assert part_segment_response.status == 206
            assert check_part_is_moof_mdat(await part_segment_response.read())

    stream_worker_sync.resume()

    # Stop stream, if it hasn't quit already
    stream.stop()

    # Ensure playlist not accessible after stream ends
    fail_response = await hls_client.get()
    assert fail_response.status == HTTP_NOT_FOUND


async def test_ll_hls_playlist_view(hass, hls_stream, stream_worker_sync):
    """Test rendering the hls playlist with 1 and 2 output segments."""
    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()
    hls = stream.add_provider(HLS_PROVIDER)

    # Add 2 complete segments to output
    for sequence in range(2):
        segment = create_segment(sequence=sequence)
        hls.put(segment)
        for part in create_parts(SEQUENCE_BYTES):
            segment.async_add_part(part, 0)
            hls.part_put()
        complete_segment(segment)
    await hass.async_block_till_done()

    hls_client = await hls_stream(stream)

    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200
    assert await resp.text() == make_playlist(
        sequence=0,
        segments=[
            make_segment_with_parts(
                i, len(segment.parts_by_byterange), PART_INDEPENDENT_PERIOD
            )
            for i in range(2)
        ],
        hint=make_hint(2, 0),
        part_target_duration=hls.stream_settings.part_target_duration,
    )

    # add one more segment
    segment = create_segment(sequence=2)
    hls.put(segment)
    for part in create_parts(SEQUENCE_BYTES):
        segment.async_add_part(part, 0)
        hls.part_put()
    complete_segment(segment)

    await hass.async_block_till_done()
    resp = await hls_client.get("/playlist.m3u8")
    assert resp.status == 200
    assert await resp.text() == make_playlist(
        sequence=0,
        segments=[
            make_segment_with_parts(
                i, len(segment.parts_by_byterange), PART_INDEPENDENT_PERIOD
            )
            for i in range(3)
        ],
        hint=make_hint(3, 0),
        part_target_duration=hls.stream_settings.part_target_duration,
    )

    stream_worker_sync.resume()
    stream.stop()


async def test_ll_hls_msn(hass, hls_stream, stream_worker_sync, hls_sync):
    """Test that requests using _HLS_msn get held and returned or rejected."""
    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()

    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Create 4 requests for sequences 0 through 3
    # 0 and 1 should hold then go through and 2 and 3 should fail immediately.

    hls_sync.reset_request_pool(4)
    msn_requests = asyncio.gather(
        *(hls_client.get(f"/playlist.m3u8?_HLS_msn={i}") for i in range(4))
    )

    for sequence in range(3):
        await hls_sync.wait_for_handler()
        segment = Segment(sequence=sequence, duration=SEGMENT_DURATION)
        hls.put(segment)

    msn_responses = await msn_requests

    assert msn_responses[0].status == 200
    assert msn_responses[1].status == 200
    assert msn_responses[2].status == 400
    assert msn_responses[3].status == 400

    # Sequence number is now 2. Create six more requests for sequences 0 through 5.
    # Calls for msn 0 through 4 should work, 5 should fail.

    hls_sync.reset_request_pool(6)
    msn_requests = asyncio.gather(
        *(hls_client.get(f"/playlist.m3u8?_HLS_msn={i}") for i in range(6))
    )
    for sequence in range(3, 6):
        await hls_sync.wait_for_handler()
        segment = Segment(sequence=sequence, duration=SEGMENT_DURATION)
        hls.put(segment)

    msn_responses = await msn_requests
    assert msn_responses[0].status == 200
    assert msn_responses[1].status == 200
    assert msn_responses[2].status == 200
    assert msn_responses[3].status == 200
    assert msn_responses[4].status == 200
    assert msn_responses[5].status == 400

    stream_worker_sync.resume()


async def test_ll_hls_playlist_bad_msn_part(hass, hls_stream, stream_worker_sync):
    """Test some playlist requests with invalid _HLS_msn/_HLS_part."""

    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()

    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # If the Playlist URI contains an _HLS_part directive but no _HLS_msn
    # directive, the Server MUST return Bad Request, such as HTTP 400.

    assert (await hls_client.get("/playlist.m3u8?_HLS_part=1")).status == 400

    # Seed hls with 1 complete segment and 1 in process segment
    segment = create_segment(sequence=0)
    hls.put(segment)
    for part in create_parts(SEQUENCE_BYTES):
        segment.async_add_part(part, 0)
        hls.part_put()
    complete_segment(segment)

    segment = create_segment(sequence=1)
    hls.put(segment)
    remaining_parts = create_parts(SEQUENCE_BYTES)
    num_completed_parts = len(remaining_parts) // 2
    for part in remaining_parts[:num_completed_parts]:
        segment.async_add_part(part, 0)

    # If the _HLS_msn is greater than the Media Sequence Number of the last
    # Media Segment in the current Playlist plus two, or if the _HLS_part
    # exceeds the last Partial Segment in the current Playlist by the
    # Advance Part Limit, then the server SHOULD immediately return Bad
    # Request, such as HTTP 400.  The Advance Part Limit is three divided
    # by the Part Target Duration if the Part Target Duration is less than
    # one second, or three otherwise.

    # Current sequence number is 1 and part number is num_completed_parts-1
    # The following two tests should fail immediately:
    # - request with a _HLS_msn of 4
    # - request with a _HLS_msn of 1 and a _HLS_part of num_completed_parts-1+advance_part_limit
    assert (await hls_client.get("/playlist.m3u8?_HLS_msn=4")).status == 400
    assert (
        await hls_client.get(
            f"/playlist.m3u8?_HLS_msn=1&_HLS_part={num_completed_parts-1+hass.data[DOMAIN][ATTR_SETTINGS].hls_advance_part_limit}"
        )
    ).status == 400
    stream_worker_sync.resume()


async def test_ll_hls_playlist_rollover_part(
    hass, hls_stream, stream_worker_sync, hls_sync
):
    """Test playlist request rollover."""

    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()

    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Seed hls with 1 complete segment and 1 in process segment
    for sequence in range(2):
        segment = create_segment(sequence=sequence)
        hls.put(segment)

        for part in create_parts(SEQUENCE_BYTES):
            segment.async_add_part(part, 0)
            hls.part_put()
        complete_segment(segment)

    await hass.async_block_till_done()

    hls_sync.reset_request_pool(4)
    segment = hls.get_segment(1)
    # the first request corresponds to the last part of segment 1
    # the remaining requests correspond to part 0 of segment 2
    requests = asyncio.gather(
        *(
            [
                hls_client.get(
                    f"/playlist.m3u8?_HLS_msn=1&_HLS_part={len(segment.parts_by_byterange)-1}"
                ),
                hls_client.get(
                    f"/playlist.m3u8?_HLS_msn=1&_HLS_part={len(segment.parts_by_byterange)}"
                ),
                hls_client.get(
                    f"/playlist.m3u8?_HLS_msn=1&_HLS_part={len(segment.parts_by_byterange)+1}"
                ),
                hls_client.get("/playlist.m3u8?_HLS_msn=2&_HLS_part=0"),
            ]
        )
    )

    await hls_sync.wait_for_handler()

    segment = create_segment(sequence=2)
    hls.put(segment)
    await hass.async_block_till_done()

    remaining_parts = create_parts(SEQUENCE_BYTES)
    segment.async_add_part(remaining_parts.pop(0), 0)
    hls.part_put()

    await hls_sync.wait_for_handler()

    different_response, *same_responses = await requests

    assert different_response.status == 200
    assert all(response.status == 200 for response in same_responses)
    different_playlist = await different_response.read()
    same_playlists = [await response.read() for response in same_responses]
    assert different_playlist != same_playlists[0]
    assert all(playlist == same_playlists[0] for playlist in same_playlists[1:])

    stream_worker_sync.resume()


async def test_ll_hls_playlist_msn_part(hass, hls_stream, stream_worker_sync, hls_sync):
    """Test that requests using _HLS_msn and _HLS_part get held and returned."""

    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()

    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Seed hls with 1 complete segment and 1 in process segment
    segment = create_segment(sequence=0)
    hls.put(segment)
    for part in create_parts(SEQUENCE_BYTES):
        segment.async_add_part(part, 0)
        hls.part_put()
    complete_segment(segment)

    segment = create_segment(sequence=1)
    hls.put(segment)
    remaining_parts = create_parts(SEQUENCE_BYTES)
    num_completed_parts = len(remaining_parts) // 2
    for part in remaining_parts[:num_completed_parts]:
        segment.async_add_part(part, 0)
    del remaining_parts[:num_completed_parts]

    # Make requests for all the part segments up to n+ADVANCE_PART_LIMIT
    hls_sync.reset_request_pool(
        num_completed_parts
        + int(-(-hass.data[DOMAIN][ATTR_SETTINGS].hls_advance_part_limit // 1))
    )
    msn_requests = asyncio.gather(
        *(
            hls_client.get(f"/playlist.m3u8?_HLS_msn=1&_HLS_part={i}")
            for i in range(
                num_completed_parts
                + int(-(-hass.data[DOMAIN][ATTR_SETTINGS].hls_advance_part_limit // 1))
            )
        )
    )

    while remaining_parts:
        await hls_sync.wait_for_handler()
        segment.async_add_part(remaining_parts.pop(0), 0)
        hls.part_put()

    msn_responses = await msn_requests

    # All the responses should succeed except the last one which fails
    assert all(response.status == 200 for response in msn_responses[:-1])
    assert msn_responses[-1].status == 400

    stream_worker_sync.resume()


async def test_get_part_segments(hass, hls_stream, stream_worker_sync, hls_sync):
    """Test requests for part segments and hinted parts."""
    await async_setup_component(
        hass,
        "stream",
        {
            "stream": {
                CONF_LL_HLS: True,
                CONF_SEGMENT_DURATION: SEGMENT_DURATION,
                CONF_PART_DURATION: TEST_PART_DURATION,
            }
        },
    )

    stream = create_stream(hass, STREAM_SOURCE, {})
    stream_worker_sync.pause()

    hls = stream.add_provider(HLS_PROVIDER)

    hls_client = await hls_stream(stream)

    # Seed hls with 1 complete segment and 1 in process segment
    segment = create_segment(sequence=0)
    hls.put(segment)
    for part in create_parts(SEQUENCE_BYTES):
        segment.async_add_part(part, 0)
        hls.part_put()
    complete_segment(segment)

    segment = create_segment(sequence=1)
    hls.put(segment)
    remaining_parts = create_parts(SEQUENCE_BYTES)
    num_completed_parts = len(remaining_parts) // 2
    for _ in range(num_completed_parts):
        segment.async_add_part(remaining_parts.pop(0), 0)

    # Make requests for all the existing part segments
    # These should succeed with a status of 206
    requests = asyncio.gather(
        *(
            hls_client.get(
                "/segment/1.m4s",
                headers={
                    "Range": f"bytes={http_range_from_part(part)[1]}-"
                    + str(
                        http_range_from_part(part)[0]
                        + http_range_from_part(part)[1]
                        - 1
                    )
                },
            )
            for part in range(num_completed_parts)
        )
    )
    responses = await requests
    assert all(response.status == 206 for response in responses)
    assert all(
        responses[part].headers["Content-Range"]
        == f"bytes {http_range_from_part(part)[1]}-"
        + str(http_range_from_part(part)[0] + http_range_from_part(part)[1] - 1)
        + "/*"
        for part in range(num_completed_parts)
    )
    parts = list(segment.parts_by_byterange.values())
    assert all(
        [await responses[i].read() == parts[i].data for i in range(len(responses))]
    )

    # Make some non standard range requests.
    # Request past end of previous closed segment
    # Request should succeed but length will be limited to the segment length
    response = await hls_client.get(
        "/segment/0.m4s",
        headers={"Range": f"bytes=0-{hls.get_segment(0).data_size+1}"},
    )
    assert response.status == 206
    assert (
        response.headers["Content-Range"]
        == f"bytes 0-{hls.get_segment(0).data_size-1}/{hls.get_segment(0).data_size}"
    )
    assert (await response.read()) == hls.get_segment(0).get_data()

    # Request with start range past end of current segment
    # Since this is beyond the data we have (the largest starting position will be
    # from a hinted request, and even that will have a starting position at
    # segment.data_size), we expect a 416.
    response = await hls_client.get(
        "/segment/1.m4s",
        headers={"Range": f"bytes={segment.data_size+1}-{VERY_LARGE_LAST_BYTE_POS}"},
    )
    assert response.status == 416

    # Request for next segment which has not yet been hinted (we will only hint
    # for this segment after segment 1 is complete).
    # This should fail, but it will hold for one more part_put before failing.
    hls_sync.reset_request_pool(1)
    request = asyncio.create_task(
        hls_client.get(
            "/segment/2.m4s", headers={"Range": f"bytes=0-{VERY_LARGE_LAST_BYTE_POS}"}
        )
    )
    await hls_sync.wait_for_handler()
    hls.part_put()
    response = await request
    assert response.status == 404

    # Make valid request for the current hint. This should succeed, but since
    # it is open ended, it won't finish until the segment is complete.
    hls_sync.reset_request_pool(1)
    request_start = segment.data_size
    request = asyncio.create_task(
        hls_client.get(
            "/segment/1.m4s",
            headers={"Range": f"bytes={request_start}-{VERY_LARGE_LAST_BYTE_POS}"},
        )
    )
    # Put the remaining parts and complete the segment
    while remaining_parts:
        await hls_sync.wait_for_handler()
        # Put one more part segment
        segment.async_add_part(remaining_parts.pop(0), 0)
        hls.part_put()
    complete_segment(segment)
    # Check the response
    response = await request
    assert response.status == 206
    assert (
        response.headers["Content-Range"]
        == f"bytes {request_start}-{VERY_LARGE_LAST_BYTE_POS}/*"
    )
    assert await response.read() == SEQUENCE_BYTES[request_start:]

    # Now the hint should have moved to segment 2
    # The request for segment 2 which failed before should work now
    # Also make an equivalent request with no Range parameters that
    # will return the same content but with different headers
    hls_sync.reset_request_pool(2)
    requests = asyncio.gather(
        hls_client.get(
            "/segment/2.m4s", headers={"Range": f"bytes=0-{VERY_LARGE_LAST_BYTE_POS}"}
        ),
        hls_client.get("/segment/2.m4s"),
    )
    # Put an entire segment and its parts.
    segment = create_segment(sequence=2)
    hls.put(segment)
    remaining_parts = create_parts(ALT_SEQUENCE_BYTES)
    for part in remaining_parts:
        await hls_sync.wait_for_handler()
        segment.async_add_part(part, 0)
        hls.part_put()
    complete_segment(segment)
    # Check the response
    responses = await requests
    assert responses[0].status == 206
    assert (
        responses[0].headers["Content-Range"] == f"bytes 0-{VERY_LARGE_LAST_BYTE_POS}/*"
    )
    assert responses[1].status == 200
    assert "Content-Range" not in responses[1].headers
    assert (
        await response.read() == ALT_SEQUENCE_BYTES[: hls.get_segment(2).data_size]
        for response in responses
    )

    stream_worker_sync.resume()
