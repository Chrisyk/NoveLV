from pathlib import Path

import pytest

from flaskr import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
        }
    )
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _create_novel_file(name: str, content: str = "sample"):
    novels_dir = Path.cwd() / "novels"
    novels_dir.mkdir(parents=True, exist_ok=True)
    fp = novels_dir / name
    fp.write_text(content, encoding="utf-8")
    return fp


def test_delete_requires_post(client):
    response = client.get("/delete/example.txt")
    assert response.status_code == 405


def test_delete_rejects_missing_csrf(client):
    fp = _create_novel_file("csrf_missing.txt")

    response = client.post(f"/delete/{fp.name}", data={}, follow_redirects=True)

    assert response.status_code == 200
    assert fp.exists()
    assert b"Invalid or missing security token" in response.data


def test_delete_rejects_bad_filename_path_traversal(client):
    _ = client.get("/")
    with client.session_transaction() as sess:
        token = sess.get("_novel_delete_csrf")

    response = client.post(
        "/delete/..",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Invalid file name" in response.data


def test_delete_success_with_valid_csrf(client):
    fp = _create_novel_file("ok_delete.txt")

    _ = client.get("/")
    with client.session_transaction() as sess:
        token = sess.get("_novel_delete_csrf")

    response = client.post(
        f"/delete/{fp.name}",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert not fp.exists()
    assert b"deleted successfully" in response.data
