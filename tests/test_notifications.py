"""
tests/test_notifications.py — Mixtape

Tests for notification generation logic.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, add_to_playlist, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_song(app):
    """Create a song shared by one user, to be rated by another."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Test Track", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, seed_song):
    """
    When a friend rates a user's song, the sharer should receive a
    'song_rated' notification. Regression test for Bug 4.
    """
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        rater_id = seed_song["rater"].id
        song_id = seed_song["song"].id

        rate_song(user_id=rater_id, song_id=song_id, score=4)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"
        assert "rater" in notifications[0]["body"]


def test_rating_your_own_song_does_not_notify_you(app, seed_song):
    """A user rating their own song should not generate a self-notification."""
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        song_id = seed_song["song"].id

        rate_song(user_id=sharer_id, song_id=song_id, score=5)

        notifications = get_notifications(sharer_id)
        assert notifications == []


def test_re_rating_a_song_notifies_again(app, seed_song):
    """Updating an existing rating should still notify the sharer."""
    with app.app_context():
        sharer_id = seed_song["sharer"].id
        rater_id = seed_song["rater"].id
        song_id = seed_song["song"].id

        rate_song(user_id=rater_id, song_id=song_id, score=2)
        rate_song(user_id=rater_id, song_id=song_id, score=5)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 2
