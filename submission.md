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

**Navigation Strategy:**
1. Reproduced the report mentally first: a user listens Saturday, then Sunday — one day apart, so the streak should increment, not reset.
2. Entered at `routes/songs.py :: listen(song_id)` and confirmed it does nothing but pull `user_id` from the request body and call `streak_service.record_listening_event(user_id, song_id)` — no branching in the route, so the route was ruled out.
3. In `record_listening_event()`, first hypothesis was a timestamp bug — maybe a late-Saturday-night listen gets recorded with Sunday's date depending on timezone handling. Checked how `now` is produced (`datetime.now(timezone.utc)`, computed once and passed to both the `ListeningEvent` row and `update_listening_streak`) — no separate computation that could drift. Ruled out.
4. Moved into `update_listening_streak()`. Second hypothesis was the `days_since_last` arithmetic itself (e.g. comparing full datetimes instead of dates). Hand-computed `(today - last_date).days` for a Sat→Sun listen — 1 day, correctly routes into the `days_since_last == 1` branch. Ruled out.
5. With both the entry point and the day math cleared, the only remaining place a reset could originate was the branch condition itself. Read the `elif` line term-by-term instead of skimming past it: `elif days_since_last == 1 and today.weekday() != 6:` — the second clause has nothing to do with elapsed days and evaluates `False` specifically when `today` is a Sunday (`weekday() == 6`), which falls through to the `else` branch and resets the streak.
6. Checked `tests/test_streaks.py` to see why this had shipped unnoticed — the existing cases cover same-day, consecutive-day, and skipped-day, but none pin the test date to a specific weekday, so the Sunday branch was never exercised.

**File & Line:** `services/streak_service.py`, line 73

**Root Cause:** The streak-increment condition was `days_since_last == 1 and today.weekday() != 6`. `weekday() == 6` is Sunday, so the guard silently skipped the increment whenever today was Sunday — falling through to the `else` branch that resets the streak to 1. A user who listened Saturday and again Sunday would always lose their streak at the week boundary.

**Fix:** Removed the `today.weekday() != 6` guard. The only condition needed to increment is that exactly one calendar day has elapsed since the last listen.

```python
# Before
elif days_since_last == 1 and today.weekday() != 6:
# After
elif days_since_last == 1:
```

**Side-effect Check:** Removing the guard only touches the `days_since_last == 1` branch, so re-ran the full `tests/test_streaks.py` suite to confirm the other two branches (`== 0`, same-day no-op; `>= 2`, reset to 1) were untouched — all passed, including the case with a naive (non-UTC-aware) `last_listened_at`, since the `tzinfo` normalization above the branch is unrelated to the weekday check. Also checked `routes/users.py :: streak(user_id)` — it only calls `get_streak()`, a plain read of `user.listening_streak`, so it has no logic that could compensate for or depend on the old Sunday behavior.

**Commit:** `42baaaa`

---

### Bug 2: Friends Listening Now shows people from yesterday

**Issue:** The "Friends Listening Now" feed showed friends who had listened up to 24 hours ago, making it look like they were currently active when they weren't.

**Navigation Strategy:**
1. The report was specific to `/feed/<id>/listening-now` and didn't mention `/feed/<id>/activity` being wrong. Read `routes/feed.py` and confirmed the two endpoints map to two different functions, `listening_now()` → `get_friends_listening_now()` and `activity()` → `get_activity_feed()` — so the search stayed inside the first function only.
2. First hypothesis: the per-friend dedup keeps the wrong event (e.g. the first event found instead of the most recent one). Read the dedup loop: `recent_events` is queried with `order_by(desc(ListeningEvent.listened_at))` before the loop runs, and the loop keeps only the first occurrence per friend via a `seen_friends` set — since the list is already newest-first, "first occurrence" is the most recent event. That logic was correct. Ruled out.
3. Second hypothesis: `user.friends` returns stale entries (e.g. an unfriended user still showing up). Read `friend_ids = [f.id for f in user.friends]` — a direct relationship traversal with no extra filtering to get wrong. Ruled out.
4. That left the recency filter: `ListeningEvent.listened_at >= cutoff`, where `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`. Traced `RECENT_THRESHOLD` to its definition at the top of the file and found `timedelta(hours=24)` — a full day window, which is exactly wide enough for someone who listened at 9pm the night before to still show up as "listening now" at 8am.

**File & Line:** `services/feed_service.py`, line 13

**Root Cause:** `RECENT_THRESHOLD = timedelta(hours=24)` was used to filter "recent" listening events. A 24-hour window means someone who listened yesterday evening would still appear as "listening now" the next morning — a misleading 23-hour lag.

**Fix:** Changed the threshold to 30 minutes, which reflects a meaningful definition of "now."

```python
# Before
RECENT_THRESHOLD = timedelta(hours=24)
# After
RECENT_THRESHOLD = timedelta(minutes=30)
```

**Side-effect Check:** Grepped the codebase for `RECENT_THRESHOLD` to confirm it's referenced in exactly one place — the `cutoff` calculation inside `get_friends_listening_now()` — so the narrower window couldn't silently affect anything else. Specifically checked `get_activity_feed()` in the same file, since it lives right next to the function that changed: its docstring states it's "not filtered by recency," and reading its query confirmed it has no threshold or cutoff logic at all, so tightening `RECENT_THRESHOLD` has no effect on the general activity feed.

**Commit:** `23a7868`

---

### Bug 3: The same song keeps showing up twice in search

**Issue:** Songs appeared multiple times in search results. A song with three tags appeared three times; a song with one tag appeared once; a song with no tags appeared once.

**Navigation Strategy:**
1. Noted the correlation in the report before opening any file: 3 tags → 3 copies, 1 tag → 1 copy, 0 tags → 1 copy. Duplication scaling with tag count pointed at whatever join brings tag data in, not at pagination or the route layer, so went straight to `services/search_service.py`.
2. First hypothesis: the `db.or_()` filter on title/artist double-counts a song whose title and artist both match the query. Read the filter — it's a single `.filter(db.or_(...))` call producing one boolean condition per row, not a separate row per matching field. Ruled out by reasoning through what a matching row would look like: one `Song` row, one boolean result.
3. Second hypothesis, and the one that held up: the `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` clause. Worked through what that join produces against the association table — an outer join returns one row per matching row on the many side, so a song with three rows in `song_tags` yields three joined rows before any deduplication.
4. Read the rest of the query chain looking for anything that would collapse those rows back to one per song — `.group_by()`, a Python-side `set()`, `.distinct()` — and found the chain ended in `.filter(...).all()` with none of those present, confirming the duplicate rows were reaching the response unmodified.

**File & Line:** `services/search_service.py`, lines 26–35

**Root Cause:** The query used `outerjoin(song_tags, Song.id == song_tags.c.song_id)` to join the tags association table, but did not deduplicate results. An `OUTER JOIN` produces one row per matching join row — so a song with three tag entries in `song_tags` generates three rows in the result set, each becoming a separate dict in the returned list.

**Fix:** Added `.distinct()` before `.all()` so each `Song` row is returned at most once regardless of how many tags it has.

```python
# Before
.filter(...).all()
# After
.filter(...).distinct().all()
```

**Side-effect Check:** `.distinct()` operates on the columns SQLAlchemy selects for the query — here, whole `Song` rows — so the concern was whether it would also flatten the `tags` list down to a single tag per song (since `Song.tags` is a separate `lazy="subquery"` relationship, not a column in the outer-joined query). Ran a manual check seeding a song with three tags and calling `search_songs()` directly: result count was 1 (deduplicated correctly) and `result["tags"]` still returned all three tag names, confirming `.distinct()` only collapses duplicate `Song` rows and doesn't touch the separately-loaded tags relationship. Also re-ran the single-tag and no-tag cases in `tests/test_search.py` to confirm neither regressed.

**Commit:** `f0abe05`

---

### Bug 4: No notification when a friend rates my song

**Issue:** Users received a notification when a friend added their song to a playlist, but received nothing when a friend rated it.

**Navigation Strategy:**
1. One notification path (playlist-add) worked and a structurally similar one (rating) didn't, so rather than tracing either call chain from the route down, opened `services/notification_service.py` and read `add_to_playlist()` and `rate_song()` side by side to diff their shape.
2. Traced `add_to_playlist()`: look up song/adder/playlist → append song to `playlist.songs` → commit → `if song.shared_by != added_by_user_id: create_notification(...)`.
3. Traced `rate_song()` the same way: look up song/rater → upsert the `Rating` row (existing vs. new) → commit → `return rating`. No notification block after the commit — the function just ends.
4. Before concluding it was simply missing, checked the alternate explanation that the call existed but was unreachable (e.g. gated behind a condition that's always false, or placed before an early return). Grepped the file for `create_notification(` and found exactly one call site, inside `add_to_playlist` — confirming there was no dead or misplaced call in `rate_song`, just an omitted step.

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

**Side-effect Check:** Wrote and ran `tests/test_notifications.py` to exercise three adjacent cases rather than just the happy path: (1) rating a friend's song produces exactly one `song_rated` notification, (2) rating your own song produces zero notifications (confirms the `shared_by != user_id` guard still works), and (3) re-rating an already-rated song (the `existing` branch in `rate_song`, which updates rather than inserts a `Rating` row) still fires a notification on each call rather than only the first. Also re-read `add_to_playlist()` to confirm it was untouched — it has its own independent `create_notification` call and doesn't share any state with `rate_song()` — and re-ran the full suite (16 passed) to confirm the streak, search, and playlist tests were unaffected by editing this file.

**Commit:** `a16818b`

---

### Bug 5: The last song in a playlist never shows up

**Issue:** Playlists always appeared to be missing their final song. A 5-song playlist showed 4 songs; a 1-song playlist showed nothing.

**Navigation Strategy:**
1. The "1-song playlist shows nothing" detail was the key clue: a property-based filtering bug wouldn't behave differently just because the list only has one element, but a positional bug (off-by-one slice, stray `.limit()`) would. That framed the search around list construction, not filtering.
2. Entered at `routes/playlists.py :: get_songs(playlist_id)` and confirmed it does nothing but call `playlist_service.get_playlist_songs(playlist_id)` and return the result directly — no slicing at the route layer. Ruled out.
3. In `get_playlist_songs()`, first hypothesis was the query itself — a wrong sort column or a stray `.limit()` that clips the last row. Read the query: `.join(playlist_entries, ...).filter(playlist_entries.c.playlist_id == playlist_id).order_by(asc(playlist_entries.c.position)).all()` — no `.limit()`, and the ordering matches what the docstring promises. Also noted the docstring literally says "this function returns all songs in the playlist," which didn't match the reported behavior — a sign the bug was downstream of the query, not in it.
4. That left the one-line return statement, easy to skim past. Read it literally instead of assuming `for song in songs` meant all of `songs`, and found `for song in songs[:-1]` — a slice that drops the last element regardless of list length, accounting for both the 5→4 and 1→0 symptoms.

**File & Line:** `services/playlist_service.py`, line 66

**Root Cause:** After querying and ordering the songs, the return statement used the slice `songs[:-1]`, which in Python returns every element *except* the last one. This is an off-by-one error — likely a mistaken edit (perhaps intended as `songs[-1:]` to get only the last, or just `songs`).

**Fix:** Removed the slice so all query results are returned.

```python
# Before
return [song.to_dict() for song in songs[:-1]]
# After
return [song.to_dict() for song in songs]
```

**Side-effect Check:** Ran `tests/test_playlists.py` to confirm three related behaviors beyond the raw count: ordering is still correct (`test_playlist_returns_songs_in_order` — position 1 through 5 in sequence, so removing the slice didn't disturb `order_by`), a genuinely empty playlist still returns `[]` without raising (`test_empty_playlist_returns_empty_list` — rules out the fix turning a zero-song edge case into an index error), and `get_playlist()` (playlist metadata only) is a separate function that never calls `get_playlist_songs()`, so it was unaffected by the change.

**Commit:** `55cdc49`
