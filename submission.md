# Project 5 Submission — Mixtape Bug Hunt

## Codebase Map

### Key Files

| File | Responsibility |
|------|---------------|
| `app.py` | Flask application factory (`create_app`). Initializes SQLAlchemy, registers the four blueprints, and calls `db.create_all()` on startup. |
| `models.py` | All SQLAlchemy models: `User`, `Song`, `Tag`, `Playlist`, `ListeningEvent`, `Rating`, `Notification`. Also defines three association tables: `friendships` (User↔User), `song_tags` (Song↔Tag), `playlist_entries` (Playlist↔Song with position). |
| `routes/songs.py` | Endpoints: `GET /songs/search`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`. Delegates to `search_service`, `notification_service`, and `streak_service`. |
| `routes/playlists.py` | Endpoints: `POST /playlists/`, `GET /playlists/<id>`, `GET /playlists/<id>/songs`, `POST /playlists/<id>/songs`. Delegates to `playlist_service` and `notification_service`. |
| `routes/users.py` | Endpoints: `GET /users/<id>`, `GET /users/<id>/streak`, `GET /users/<id>/notifications`, `POST /users/notifications/<id>/read`. Delegates to `streak_service` and `notification_service`. |
| `routes/feed.py` | Endpoints: `GET /feed/<id>/listening-now`, `GET /feed/<id>/activity`. Delegates to `feed_service`. |
| `services/streak_service.py` | `record_listening_event()` creates a `ListeningEvent` row and calls `update_listening_streak()`, which compares today's date against `user.last_listened_at` to increment, hold, or reset the streak. |
| `services/feed_service.py` | `get_friends_listening_now()` looks up the user's friends via the `friendships` association, queries `ListeningEvent` rows within a recency threshold, and deduplicates to one entry per friend. |
| `services/search_service.py` | `search_songs()` queries `Song` with a case-insensitive `ILIKE` filter on title and artist, outer-joining `song_tags` to make tag data available. |
| `services/notification_service.py` | `rate_song()` upserts a `Rating` row. `add_to_playlist()` appends a song to a playlist's `songs` relationship. Both can call `create_notification()` to write a `Notification` row for the song's original sharer. |
| `services/playlist_service.py` | `get_playlist_songs()` queries `Song` joined through `playlist_entries`, ordered by `position`. `create_playlist()` and `get_user_playlists()` manage `Playlist` rows. |

### Data Flow: User listens to a song

```
POST /songs/<song_id>/listen  { "user_id": "..." }
  └── routes/songs.py :: listen()
        └── streak_service.record_listening_event(user_id, song_id)
              ├── db.session.get(User, user_id)          # verify user exists
              ├── ListeningEvent(user_id, song_id, now)  # write event row
              ├── update_listening_streak(user, now)
              │     ├── days_since_last = (today - last_date).days
              │     ├── 0 days  → no change
              │     ├── 1 day   → user.listening_streak += 1
              │     └── 2+ days → user.listening_streak = 1
              └── db.session.commit()
```

---

## Root Cause Analysis

---

### Bug 1: My listening streak keeps resetting

**Issue:** Users reported their listening streak reset to 1 every Sunday, even when they had listened the day before (Saturday).

**File & Line:** `services/streak_service.py`, line 73

**Root Cause:** The streak-increment condition was `days_since_last == 1 and today.weekday() != 6`. `weekday() == 6` is Sunday, so the guard silently skipped the increment whenever today was Sunday — falling through to the `else` branch that resets the streak to 1. A user who listened Saturday and again Sunday would always lose their streak at the week boundary.

**Fix:** Removed the `today.weekday() != 6` guard. The only condition needed to increment is that exactly one calendar day has elapsed since the last listen.

```python
# Before
elif days_since_last == 1 and today.weekday() != 6:
# After
elif days_since_last == 1:
```

**Commit:** `42baaaa`

---

### Bug 2: Friends Listening Now shows people from yesterday

**Issue:** The "Friends Listening Now" feed showed friends who had listened up to 24 hours ago, making it look like they were currently active when they weren't.

**File & Line:** `services/feed_service.py`, line 13

**Root Cause:** `RECENT_THRESHOLD = timedelta(hours=24)` was used to filter "recent" listening events. A 24-hour window means someone who listened yesterday evening would still appear as "listening now" the next morning — a misleading 23-hour lag.

**Fix:** Changed the threshold to 30 minutes, which reflects a meaningful definition of "now."

```python
# Before
RECENT_THRESHOLD = timedelta(hours=24)
# After
RECENT_THRESHOLD = timedelta(minutes=30)
```

**Commit:** `23a7868`

---

### Bug 3: The same song keeps showing up twice in search

**Issue:** Songs appeared multiple times in search results. A song with three tags appeared three times; a song with one tag appeared once; a song with no tags appeared once.

**File & Line:** `services/search_service.py`, lines 26–35

**Root Cause:** The query used `outerjoin(song_tags, Song.id == song_tags.c.song_id)` to join the tags association table, but did not deduplicate results. An `OUTER JOIN` produces one row per matching join row — so a song with three tag entries in `song_tags` generates three rows in the result set, each becoming a separate dict in the returned list.

**Fix:** Added `.distinct()` before `.all()` so each `Song` row is returned at most once regardless of how many tags it has.

```python
# Before
.filter(...).all()
# After
.filter(...).distinct().all()
```

**Commit:** `f0abe05`

---

### Bug 4: No notification when a friend rates my song

**Issue:** Users received a notification when a friend added their song to a playlist, but received nothing when a friend rated it.

**File & Line:** `services/notification_service.py`, `rate_song()` function (~line 108)

**Root Cause:** The `add_to_playlist` function correctly called `create_notification()` after saving its action. The `rate_song` function saved the rating and committed to the database but never called `create_notification` — the notification step was simply omitted.

**Fix:** After the commit, added a `create_notification` call that notifies `song.shared_by` when someone else rates their song. The self-rating case (`song.shared_by == user_id`) is excluded so users don't notify themselves.

```python
if song.shared_by != user_id:
    create_notification(
        user_id=song.shared_by,
        notification_type="song_rated",
        body=f"{rater.username} rated your song '{song.title}' {score}/5.",
    )
```

**Commit:** `a16818b`

---

### Bug 5: The last song in a playlist never shows up

**Issue:** Playlists always appeared to be missing their final song. A 5-song playlist showed 4 songs; a 1-song playlist showed nothing.

**File & Line:** `services/playlist_service.py`, line 66

**Root Cause:** After querying and ordering the songs, the return statement used the slice `songs[:-1]`, which in Python returns every element *except* the last one. This is an off-by-one error — likely a mistaken edit (perhaps intended as `songs[-1:]` to get only the last, or just `songs`).

**Fix:** Removed the slice so all query results are returned.

```python
# Before
return [song.to_dict() for song in songs[:-1]]
# After
return [song.to_dict() for song in songs]
```

**Commit:** `55cdc49`
