"""章节标题与对应章节大纲标题的一致性测试。"""

from test_smoke import _auth_headers, client, setup_db  # noqa: F401


def test_update_chapter_title_syncs_outline_title(setup_db):
    headers = _auth_headers("chapter_title_sync", "chapter_title_sync_pass")
    project_response = client.post(
        "/api/projects",
        json={"title": "标题同步测试"},
        headers=headers,
    )
    project_id = project_response.json()["id"]

    chapter_response = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "旧章节标题", "sequence_number": 1},
        headers=headers,
    )
    assert chapter_response.status_code == 200

    outline_response = client.post(
        f"/api/projects/{project_id}/outlines",
        json={
            "sequence_number": 1,
            "title": "旧章节标题",
            "summary": "本章摘要",
        },
        headers=headers,
    )
    assert outline_response.status_code == 200

    renamed_response = client.patch(
        f"/api/projects/{project_id}/chapters/1",
        json={"title": "新章节标题"},
        headers=headers,
    )
    assert renamed_response.status_code == 200
    assert renamed_response.json()["title"] == "新章节标题"

    outlines_response = client.get(
        f"/api/projects/{project_id}/outlines",
        headers=headers,
    )
    assert outlines_response.status_code == 200
    assert outlines_response.json()[0]["title"] == "新章节标题"
