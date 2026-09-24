"""Mission Journal (STO-Style):

Replaces the Mission Log window (`Client.py`'s "Missions" panel, opened via
the J hotkey or the Menu popup) with a much bigger, three-part layout closer
to Star Trek Online's mission journal:

  * A category sidebar (grouped by the mission's own `category` field --
    the same field the docked-station board already sorts by, so no new
    taxonomy is invented) instead of a flat Ongoing/Completed row list.
  * A persistent detail pane. Selecting a mission in the sidebar shows its
    description/objectives/rewards/buttons inline, in the same window --
    your own missions no longer pop a separate centered modal to see or act
    on them (station-offered Accept/Refuse and the "mission complete" story
    popup are untouched; those aren't the Mission Log window).
  * A trackable mission: a star on the detail pane's button row marks one
    mission as tracked, and a small always-on HUD widget (top-left) shows
    its current objective live, independent of whether the journal window
    is open. Tracked state persists across sessions (small JSON file next
    to the mod's settings).

Integration strategy (why it's built this way):

  * `_draw_mission_log_panel` (render_mixin.py) is fully replaced -- the
    new layout has nothing in common with the old one's geometry. It keeps
    publishing `self._mission_log_panel_rect`, `_mission_log_close_rect`,
    `_mission_log_tab_rects` (repurposed as the Ongoing/Completed segmented
    control, same two keys "ongoing"/"completed") and
    `_mission_log_search_rect` with the SAME meaning Client.py's own native
    click handler already expects, so the close button, tab switching, and
    the search box's focus/typing keep working entirely through the native
    code path (Client.py's MOUSEBUTTONDOWN handler + the K_* text-edit
    block) with zero extra patching. `self._mission_log_row_rects` is
    deliberately left EMPTY every frame: that's what stops the native
    "row click -> open_mission_detail(...)" handler from ever firing and
    popping the old modal for a sidebar click -- this mod owns sidebar/
    detail-pane clicks itself via the client.event hook instead, and the
    native fallback for an unmatched click inside the panel (unfocus the
    search box) is harmless when it fires on top of that.
  * The detail pane's Abandon/Claim/Autopilot buttons call the exact same
    entry points the old modal's buttons did (`_send_fn("ABANDON_MISSION
    ...")` etc., `_begin_mission_autopilot(...)`), so all the existing
    server-driven plumbing (on_mission_accepted/_completed/_progress/...)
    keeps working unchanged -- this mod only changes how the data is drawn
    and clicked, not how missions are accepted/tracked/completed.
  * The HUD tracker is a plain `client.draw` hook -- fully additive, no
    patch needed, since nothing native draws anything like it.
"""
import json
import os
import time

try:
    from station_mission_rows import visible_mission_rewards
except Exception:
    def visible_mission_rewards(rewards):
        return [r for r in (rewards or []) if r.get("type") != "take_item"]

_patched_class = None
_original_draw_mission_log_panel = None

_state_path = [None]
_tracked_mission_id = [None]

# Selection + scroll state for the journal window. Reset on tab change.
_selected_mission_id = [None]
_last_tab = [None]
_sidebar_scroll = [0]          # mirrors self._mission_log_scroll (native)
_detail_scroll = [0]

# Hit-test rects populated fresh every draw frame, consumed by _on_event.
_sidebar_hit = []      # [(rect, mission_id)]
_category_hit = []     # [(rect, category_key)]
_detail_hit = {}        # {"track": rect|None, "abandon": ..., "claim": ..., "travel": ...}
_sidebar_rect = [None]
_detail_rect = [None]
_tracker_hit = [None]  # untrack (x) button rect on the HUD tracker widget

_collapsed_categories = set()

_READY_CATEGORY = "★ Ready to Claim"   # sorts first, never collides with real names


def _load_state():
    path = _state_path[0]
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _tracked_mission_id[0] = data.get("tracked_mission_id")
        _collapsed_categories.clear()
        _collapsed_categories.update(data.get("collapsed_categories") or [])
    except Exception:
        pass


def _save_state():
    path = _state_path[0]
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "tracked_mission_id": _tracked_mission_id[0],
                "collapsed_categories": sorted(_collapsed_categories),
            }, f)
    except Exception:
        pass


def _set_tracked(mission_id):
    _tracked_mission_id[0] = None if _tracked_mission_id[0] == mission_id else mission_id
    _save_state()


def _find_mission(host, mission_id):
    if not mission_id:
        return None, None
    with host._lock:
        for m in host._my_missions_active:
            if m.get("mission_id") == mission_id:
                return dict(m), "ongoing"
        for m in host._my_missions_completed:
            if m.get("mission_id") == mission_id:
                return dict(m), "completed"
    return None, None


def _wrap(text, fnt, avail_w):
    out = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        line = ""
        for w in words:
            test = w if not line else line + " " + w
            if fnt.size(test)[0] <= avail_w:
                line = test
            else:
                if line:
                    out.append(line)
                line = w
        out.append(line)
    return out


def _grouped(missions, tab):
    """Group missions by category. For the ongoing tab, ready-to-claim
    missions are pulled into a synthetic leading category so they read like
    STO's "ready" queue instead of being buried alphabetically."""
    groups = {}
    for m in missions:
        if tab == "ongoing" and m.get("ready_to_claim"):
            key = _READY_CATEGORY
        else:
            key = m.get("category") or "Missions"
        groups.setdefault(key, []).append(m)
    for key, rows in groups.items():
        rows.sort(key=lambda m: (not m.get("ready_to_claim"), m.get("name", "")))

    def _cat_sort_key(key):
        return (key != _READY_CATEGORY, key)
    return [(key, groups[key]) for key in sorted(groups, key=_cat_sort_key)]


def _travel_target(mission):
    for o in (mission.get("objectives") or []):
        if o.get("done"):
            continue
        loc = o.get("location")
        if loc and loc.get("system_name"):
            return loc
    return None


def _make_draw_mission_log_panel(pygame):
    def draw(self, ctx):
        screen = ctx.screen
        win_w = ctx.win_w
        win_h = ctx.win_h
        F = self._F

        if not self._mission_log_open:
            self._mission_log_panel_rect = None
            self._mission_log_close_rect = None
            self._mission_log_tab_rects = {}
            self._mission_log_search_rect = None
            self._mission_log_row_rects = []
            _sidebar_rect[0] = None
            _detail_rect[0] = None
            _sidebar_hit.clear()
            _category_hit.clear()
            _detail_hit.clear()
            return

        JW, JH = 940, 620
        HDR_H = 32
        TAB_H = 28
        SEARCH_H = 26
        PAD = 12
        SIDEBAR_W = 300
        CAT_H = 22
        ROW_H = 32

        jx = (win_w - JW) // 2
        jy = (win_h - JH) // 2
        self._mission_log_panel_rect = pygame.Rect(jx, jy, JW, JH)

        self._draw_screen_tint(screen, win_w, win_h, (0, 0, 0, 110))

        bg = pygame.Surface((JW, JH), pygame.SRCALPHA)
        bg.fill((8, 12, 26, 245))
        screen.blit(bg, (jx, jy))
        pygame.draw.rect(screen, (220, 180, 60), (jx, jy, JW, JH), 2, border_radius=6)

        # Header
        pygame.draw.rect(screen, (40, 30, 8), (jx, jy, JW, HDR_H))
        pygame.draw.rect(screen, (220, 180, 60), (jx, jy, JW, HDR_H), 1)
        title = F(14, bold=True).render("MISSION JOURNAL", True, (255, 230, 140))
        screen.blit(title, (jx + PAD, jy + (HDR_H - title.get_height()) // 2))

        X_W = 26
        close_r = pygame.Rect(jx + JW - X_W - 4, jy + 3, X_W, HDR_H - 6)
        mp = pygame.mouse.get_pos()
        close_hov = close_r.collidepoint(mp)
        pygame.draw.rect(screen, (180, 80, 80) if close_hov else (140, 60, 60),
                          close_r, border_radius=3)
        xs = F(13, bold=True).render("X", True, (255, 200, 200))
        screen.blit(xs, (close_r.x + (X_W - xs.get_width()) // 2,
                          close_r.y + (close_r.h - xs.get_height()) // 2))
        self._mission_log_close_rect = close_r

        # Segmented control (reuses native tab-switch handling verbatim --
        # same self._mission_log_tab_rects keys the old panel used).
        tab_y = jy + HDR_H
        tab_w = JW // 2
        tab_rects = {}
        with self._lock:
            n_ongoing = len(self._my_missions_active)
            n_completed = len(self._my_missions_completed)
        for i, (label, key, count) in enumerate((
                ("Ongoing", "ongoing", n_ongoing),
                ("Completed", "completed", n_completed))):
            tr = pygame.Rect(jx + i * tab_w, tab_y, tab_w, TAB_H)
            active = self._mission_log_tab == key
            bg_c = (60, 40, 10) if active else (20, 14, 4)
            bdr_c = (220, 180, 60) if active else (100, 74, 28)
            txt_c = (255, 230, 140) if active else (140, 110, 70)
            self._btn(screen, tr, fill=bg_c, border=bdr_c, border_w=1, radius=0)
            lbl = F(12, bold=True).render(f"{label}  ({count})", True, txt_c)
            screen.blit(lbl, (tr.x + (tab_w - lbl.get_width()) // 2,
                               tr.y + (TAB_H - lbl.get_height()) // 2))
            tab_rects[key] = tr
        self._mission_log_tab_rects = tab_rects

        if self._mission_log_tab != _last_tab[0]:
            _last_tab[0] = self._mission_log_tab
            _selected_mission_id[0] = None
            _detail_scroll[0] = 0

        # Search bar
        search_y = tab_y + TAB_H + PAD // 2
        search_r = pygame.Rect(jx + PAD, search_y, JW - PAD * 2, SEARCH_H)
        s_bg = (12, 16, 32) if self._mission_log_search_focused else (8, 12, 24)
        s_bdr = (220, 180, 60) if self._mission_log_search_focused else (100, 74, 28)
        pygame.draw.rect(screen, s_bg, search_r, border_radius=3)
        pygame.draw.rect(screen, s_bdr, search_r, 1, border_radius=3)
        sfnt = F(12)
        if self._mission_log_search:
            disp, col = self._mission_log_search, (255, 230, 160)
        elif self._mission_log_search_focused:
            disp, col = "", (140, 110, 70)
        else:
            disp, col = "Search mission names...", (100, 80, 50)
        ss = sfnt.render(disp, True, col)
        screen.blit(ss, (search_r.x + 6, search_r.y + (SEARCH_H - ss.get_height()) // 2))
        if self._mission_log_search_focused:
            cur = max(0, min(len(self._mission_log_search),
                              self._text_cursors.get("mission_log_search",
                                                      len(self._mission_log_search))))
            cx = search_r.x + 6 + sfnt.size(self._mission_log_search[:cur])[0] + 1
            if int(time.monotonic() * 2) % 2 == 0:
                pygame.draw.line(screen, (220, 180, 60),
                                  (cx, search_r.y + 4), (cx, search_r.bottom - 4))
        self._mission_log_search_rect = search_r

        # Body: sidebar (left) + detail pane (right)
        body_top = search_y + SEARCH_H + PAD
        body_h = jy + JH - PAD - body_top
        sidebar_r = pygame.Rect(jx + PAD, body_top, SIDEBAR_W, body_h)
        divider_x = sidebar_r.right + PAD
        detail_r = pygame.Rect(divider_x + 1, body_top,
                                JW - PAD * 2 - SIDEBAR_W - 1, body_h)
        pygame.draw.line(screen, (100, 74, 28),
                          (divider_x, body_top), (divider_x, body_top + body_h))
        _sidebar_rect[0] = sidebar_r
        _detail_rect[0] = detail_r

        with self._lock:
            src_list = (list(self._my_missions_active)
                        if self._mission_log_tab == "ongoing"
                        else list(self._my_missions_completed))
        q = self._mission_log_search.strip().lower()
        if q:
            src_list = [m for m in src_list
                        if q in m.get("name", "").lower()
                        or q in m.get("mission_id", "").lower()]

        groups = _grouped(src_list, self._mission_log_tab)

        # Auto-select the first mission if none/invalid is selected.
        ids_here = {m.get("mission_id") for _c, rows in groups for m in rows}
        if _selected_mission_id[0] not in ids_here:
            _selected_mission_id[0] = next(iter(ids_here), None) if ids_here else None

        old_clip = screen.get_clip()
        screen.set_clip(sidebar_r)

        pitch_cat = CAT_H + 2
        pitch_row = ROW_H + 2
        total_h = 0
        for _cat, rows in groups:
            total_h += pitch_cat
            if _cat not in _collapsed_categories:
                total_h += len(rows) * pitch_row
        max_scroll = max(0, total_h - body_h)
        scroll = max(0, min(self._mission_log_scroll, max_scroll))
        self._mission_log_scroll = scroll
        _sidebar_scroll[0] = scroll

        _sidebar_hit.clear()
        _category_hit.clear()
        mxs, mys = mp
        ry = sidebar_r.top - scroll

        if not groups:
            empty = ("No matching missions." if q else
                      ("No ongoing missions." if self._mission_log_tab == "ongoing"
                       else "No completed missions yet."))
            es = F(12).render(empty, True, (140, 110, 70))
            screen.blit(es, (sidebar_r.x + (SIDEBAR_W - es.get_width()) // 2,
                              sidebar_r.top + 20))

        for cat, rows in groups:
            cat_r = pygame.Rect(sidebar_r.x, ry, SIDEBAR_W, CAT_H)
            if cat_r.bottom >= sidebar_r.top and cat_r.top <= sidebar_r.bottom:
                collapsed = cat in _collapsed_categories
                is_ready_cat = cat == _READY_CATEGORY
                cat_col = (255, 220, 80) if is_ready_cat else (200, 200, 220)
                arrow = "▶" if collapsed else "▼"
                cs = F(11, bold=True).render(f"{arrow} {cat}  ({len(rows)})", True, cat_col)
                screen.blit(cs, (cat_r.x + 2, cat_r.y + (CAT_H - cs.get_height()) // 2))
                _category_hit.append((pygame.Rect(cat_r), cat))
            ry += pitch_cat
            if cat in _collapsed_categories:
                continue
            for m in rows:
                row_r = pygame.Rect(sidebar_r.x, ry, SIDEBAR_W, ROW_H)
                if row_r.bottom < sidebar_r.top or row_r.top > sidebar_r.bottom:
                    ry += pitch_row
                    _sidebar_hit.append((pygame.Rect(row_r), m.get("mission_id")))
                    continue
                mid = m.get("mission_id")
                selected = mid == _selected_mission_id[0]
                hov = row_r.collidepoint(mxs, mys) and sidebar_r.collidepoint(mxs, mys)
                ready = bool(m.get("ready_to_claim"))
                if selected:
                    rbg, rbdr = (50, 40, 10), (255, 220, 80)
                elif hov:
                    rbg, rbdr = (30, 22, 6), (150, 120, 50)
                else:
                    rbg, rbdr = (16, 12, 4), (70, 54, 22)
                pygame.draw.rect(screen, rbg, row_r, border_radius=3)
                pygame.draw.rect(screen, rbdr, row_r, 1, border_radius=3)
                name = m.get("name", mid or "?")
                ntxt_col = (255, 230, 160) if (selected or hov) else (210, 195, 150)
                ns = F(11, bold=selected).render(name, True, ntxt_col)
                nx = row_r.x + 8
                if mid == _tracked_mission_id[0]:
                    st = F(11, bold=True).render("★", True, (255, 220, 80))
                    screen.blit(st, (nx, row_r.y + (ROW_H - st.get_height()) // 2))
                    nx += st.get_width() + 4
                screen.blit(ns, (nx, row_r.y + (ROW_H - ns.get_height()) // 2))
                if self._mission_log_tab == "ongoing" and not ready:
                    objs = m.get("objectives") or []
                    if objs:
                        done = sum(1 for o in objs if o.get("done"))
                        ps = F(9).render(f"{done}/{len(objs)}", True, (150, 190, 220))
                        screen.blit(ps, (row_r.right - ps.get_width() - 6,
                                          row_r.y + (ROW_H - ps.get_height()) // 2))
                elif self._mission_log_tab == "completed":
                    tc = int(m.get("times_completed", 1))
                    if tc > 1:
                        ps = F(9).render(f"x{tc}", True, (120, 220, 180))
                        screen.blit(ps, (row_r.right - ps.get_width() - 6,
                                          row_r.y + (ROW_H - ps.get_height()) // 2))
                _sidebar_hit.append((pygame.Rect(row_r), mid))
                ry += pitch_row

        screen.set_clip(old_clip)
        # Row click never routes through the native "open modal" path --
        # keep this empty so Client.py's own click handler no-ops on it.
        self._mission_log_row_rects = []

        # ── Detail pane ──────────────────────────────────────────────
        _detail_hit.clear()
        mission, mtab = _find_mission(self, _selected_mission_id[0])
        if mission is None:
            no_sel = F(12).render("Select a mission from the list.", True, (140, 110, 70))
            screen.blit(no_sel, (detail_r.x + (detail_r.w - no_sel.get_width()) // 2,
                                  detail_r.y + 20))
            return

        BTN_H = 30
        btn_y = detail_r.bottom - BTN_H
        content_clip = pygame.Rect(detail_r.x, detail_r.top,
                                    detail_r.w, detail_r.h - BTN_H - 6)
        old_clip2 = screen.get_clip()
        screen.set_clip(content_clip)

        avail_w = detail_r.w - 8
        fnt_h = F(14, bold=True)
        fnt_cat = F(11)
        fnt_b = F(12)
        fnt_bb = F(12, bold=True)

        cy = detail_r.top - _detail_scroll[0]
        name_s = fnt_h.render(mission.get("name", "?"), True, (255, 230, 140))
        screen.blit(name_s, (detail_r.x, cy))
        cy += name_s.get_height() + 4

        cat = mission.get("category", "")
        if cat:
            cs = fnt_cat.render(cat, True, (160, 130, 70))
            screen.blit(cs, (detail_r.x, cy))
            cy += cs.get_height() + 4

        pygame.draw.line(screen, (100, 74, 28),
                          (detail_r.x, cy), (detail_r.right, cy))
        cy += 8

        desc_lines = _wrap((mission.get("description") or "").strip(), fnt_b, avail_w)
        for dl in desc_lines:
            ds = fnt_b.render(dl, True, (200, 220, 255))
            screen.blit(ds, (detail_r.x, cy))
            cy += fnt_b.get_linesize() + 2
        if desc_lines:
            cy += 4
            pygame.draw.line(screen, (100, 74, 28), (detail_r.x, cy), (detail_r.right, cy))
            cy += 8

        objs = mission.get("objectives") or []
        ohs = fnt_bb.render("Objectives", True, (255, 220, 120))
        screen.blit(ohs, (detail_r.x, cy))
        cy += ohs.get_height() + 2
        if not objs:
            ns = fnt_b.render("  (none)", True, (140, 110, 70))
            screen.blit(ns, (detail_r.x, cy))
            cy += fnt_b.get_linesize() + 2
        else:
            for o in objs:
                txt = o.get("text", "?")
                if "current" in o and "required" in o:
                    prog = f"  ({o['current']}/{o['required']})"
                    done = bool(o.get("done"))
                else:
                    prog, done = "", False
                col = (120, 220, 180) if done else (180, 220, 255)
                bullet = "[x]" if done else "[ ]"
                for line in _wrap(f"  {bullet} {txt}{prog}", fnt_b, avail_w):
                    os_ = fnt_b.render(line, True, col)
                    screen.blit(os_, (detail_r.x, cy))
                    cy += fnt_b.get_linesize() + 2
        cy += 4
        pygame.draw.line(screen, (100, 74, 28), (detail_r.x, cy), (detail_r.right, cy))
        cy += 8

        rews = visible_mission_rewards(mission.get("rewards"))
        rhs = fnt_bb.render("Rewards", True, (255, 220, 120))
        screen.blit(rhs, (detail_r.x, cy))
        cy += rhs.get_height() + 2
        if not rews:
            ns = fnt_b.render("  (none)", True, (140, 110, 70))
            screen.blit(ns, (detail_r.x, cy))
            cy += fnt_b.get_linesize() + 2
        else:
            for r in rews:
                for line in _wrap(f"  - {r.get('text', '?')}", fnt_b, avail_w):
                    rs = fnt_b.render(line, True, (150, 220, 180))
                    screen.blit(rs, (detail_r.x, cy))
                    cy += fnt_b.get_linesize() + 2

        if mtab == "ongoing" and isinstance(mission.get("time_left"), int) and mission["time_left"] > 0:
            tl = mission["time_left"]
            tl_lbl = (f"Time left: {tl // 60}m {tl % 60}s" if tl >= 60 else f"Time left: {tl}s")
            tls = fnt_b.render(tl_lbl, True, (200, 160, 60))
            screen.blit(tls, (detail_r.x, cy))
            cy += tls.get_height() + 4

        content_h = cy + _detail_scroll[0] - detail_r.top
        max_dscroll = max(0, content_h - content_clip.h)
        if _detail_scroll[0] > max_dscroll:
            _detail_scroll[0] = max_dscroll

        screen.set_clip(old_clip2)

        # Button row
        mid = mission.get("mission_id")
        travel_loc = _travel_target(mission) if mtab == "ongoing" else None
        x_cursor = detail_r.x

        def draw_btn(w, label, fill, fill_hov, bdr, txt_c):
            nonlocal x_cursor
            r = pygame.Rect(x_cursor, btn_y, w, BTN_H)
            hov = r.collidepoint(mp)
            self._btn(screen, r, fnt_bb.render(label, True, txt_c),
                       fill=fill_hov if hov else fill, border=bdr, border_w=1, radius=4)
            x_cursor += w + 6
            return r

        if mtab == "ongoing":
            tracked = mid == _tracked_mission_id[0]
            star_w = 34
            _detail_hit["track"] = draw_btn(
                star_w, "★" if tracked else "☆",
                (60, 50, 0) if tracked else (20, 20, 20),
                (90, 75, 0) if tracked else (40, 40, 40),
                (255, 220, 80) if tracked else (110, 110, 110),
                (255, 250, 180) if tracked else (200, 200, 200))

            remaining_w = detail_r.right - x_cursor
            specs = []
            if mission.get("ready_to_claim"):
                specs.append(("Claim", "claim", (60, 50, 0), (100, 80, 0),
                               (255, 220, 80), (255, 250, 180)))
            if mission.get("abandonable", True):
                specs.append(("Abandon", "abandon", (60, 30, 30), (100, 50, 50),
                               (220, 100, 100), (255, 180, 180)))
            if travel_loc is not None:
                specs.append((f"Autopilot to {travel_loc.get('label', 'destination')}",
                               "travel", (10, 50, 70), (20, 80, 110),
                               (80, 200, 240), (180, 235, 255)))
            if specs:
                gap = 6
                n = len(specs)
                w = max(80, (remaining_w - gap * (n - 1)) // n)
                for label, key, fill, hov, bdr, txt_c in specs:
                    _detail_hit[key] = draw_btn(w, label, fill, hov, bdr, txt_c)
            if not mission.get("abandonable", True):
                note = fnt_b.render("This mission can't be abandoned.", True, (220, 150, 90))
                screen.blit(note, (detail_r.x, btn_y - note.get_height() - 4))
        else:
            tc = int(mission.get("times_completed", 1))
            done_lbl = f"Completed x{tc}" if tc > 1 else "Completed"
            ds = fnt_bb.render(done_lbl, True, (120, 220, 180))
            screen.blit(ds, (detail_r.x, btn_y + (BTN_H - ds.get_height()) // 2))

    return draw


def _draw_tracker(host, pygame, screen, render_target):
    _tracker_hit[0] = None
    mid = _tracked_mission_id[0]
    if not mid:
        return
    if getattr(host, "_disconnected", False):
        return
    mission, tab = _find_mission(host, mid)
    if mission is None or tab != "ongoing":
        _tracked_mission_id[0] = None
        _save_state()
        return

    objs = mission.get("objectives") or []
    current_obj = next((o for o in objs if not o.get("done")), None)

    F = host._instrument_font
    name_f = F(11, bold=True)
    obj_f = F(10)

    x, y = host._s(12), host._s(90)
    w = host._s(260)
    pad = host._s(8)

    name_s = name_f.render(mission.get("name", "?").upper(), True, (255, 220, 120))
    lines = []
    if current_obj is not None:
        txt = current_obj.get("text", "?")
        if "current" in current_obj and "required" in current_obj:
            txt += f"  ({current_obj['current']}/{current_obj['required']})"
        lines = [txt]
    elif mission.get("ready_to_claim"):
        lines = ["Ready to claim - return to turn-in."]

    h = pad * 2 + name_s.get_height() + (len(lines) * (obj_f.get_linesize())) + host._s(2)
    box = pygame.Rect(x, y, w, h)
    surf = pygame.Surface((box.w, box.h), pygame.SRCALPHA)
    surf.fill((8, 12, 24, 200))
    pygame.draw.rect(surf, (220, 180, 60, 220), surf.get_rect(), 1, border_radius=host._s(4))
    screen.blit(surf, box.topleft)

    cy = box.y + pad
    screen.blit(name_s, (box.x + pad, cy))

    close_size = host._s(14)
    close_r = pygame.Rect(box.right - close_size - host._s(4), box.y + host._s(4),
                           close_size, close_size)
    mp = pygame.mouse.get_pos()
    hov = close_r.collidepoint(mp)
    pygame.draw.rect(screen, (100, 50, 50) if hov else (60, 40, 40), close_r, border_radius=3)
    xs = obj_f.render("x", True, (230, 200, 200))
    screen.blit(xs, (close_r.x + (close_size - xs.get_width()) // 2,
                      close_r.y + (close_size - xs.get_height()) // 2))
    _tracker_hit[0] = close_r

    cy += name_s.get_height() + host._s(2)
    for line in lines:
        ls = obj_f.render(line, True, (200, 220, 255))
        screen.blit(ls, (box.x + pad, cy))
        cy += obj_f.get_linesize()


def _on_event(host, pygame, event, screen):
    if getattr(host, "_disconnected", False) or getattr(host, "_esc_menu_open", False):
        return

    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
        tr = _tracker_hit[0]
        if tr is not None and tr.collidepoint(event.pos):
            _tracked_mission_id[0] = None
            _save_state()
            return

        if not getattr(host, "_mission_log_open", False):
            return
        pos = event.pos

        for rect, cat in _category_hit:
            if rect.collidepoint(pos):
                if cat in _collapsed_categories:
                    _collapsed_categories.discard(cat)
                else:
                    _collapsed_categories.add(cat)
                _save_state()
                return

        for rect, mid in _sidebar_hit:
            if rect.collidepoint(pos):
                _selected_mission_id[0] = mid
                _detail_scroll[0] = 0
                return

        track_r = _detail_hit.get("track")
        if track_r is not None and track_r.collidepoint(pos):
            _set_tracked(_selected_mission_id[0])
            return

        abandon_r = _detail_hit.get("abandon")
        if abandon_r is not None and abandon_r.collidepoint(pos):
            mid = _selected_mission_id[0]
            if mid:
                host._send_fn(f"ABANDON_MISSION {mid}")
            return

        claim_r = _detail_hit.get("claim")
        if claim_r is not None and claim_r.collidepoint(pos):
            mid = _selected_mission_id[0]
            if mid:
                host._send_fn(f"CLAIM_MISSION {mid}")
            return

        travel_r = _detail_hit.get("travel")
        if travel_r is not None and travel_r.collidepoint(pos):
            mission, tab = _find_mission(host, _selected_mission_id[0])
            if mission is not None:
                loc = _travel_target(mission)
                if loc:
                    host._begin_mission_autopilot(loc)
            return
        return

    if event.type == pygame.MOUSEWHEEL:
        if not getattr(host, "_mission_log_open", False):
            return
        mp = pygame.mouse.get_pos()
        dr = _detail_rect[0]
        if dr is not None and dr.collidepoint(mp):
            _detail_scroll[0] = max(0, _detail_scroll[0] - event.y * 30)
        # Sidebar scroll is left to the native wheel handler, which already
        # adjusts self._mission_log_scroll whenever the mouse is anywhere
        # over self._mission_log_panel_rect.


def _on_startup(host, pygame, screen):
    global _patched_class, _original_draw_mission_log_panel

    cls = type(host)
    _patched_class = cls
    _original_draw_mission_log_panel = cls._draw_mission_log_panel
    cls._draw_mission_log_panel = _make_draw_mission_log_panel(pygame)


def _on_draw(host, pygame, screen, render_target):
    if getattr(host, "_disconnected", False):
        return
    _draw_tracker(host, pygame, screen, render_target)


def _on_shutdown(**_kwargs):
    if _patched_class is not None and _original_draw_mission_log_panel is not None:
        _patched_class._draw_mission_log_panel = _original_draw_mission_log_panel


def apply(api):
    root = os.path.dirname(api.settings_path)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    _state_path[0] = os.path.join(root, "mission_journal_state.json")
    _load_state()

    api.on("client.startup", _on_startup)
    api.on("client.draw", _on_draw)
    api.on("client.event", _on_event)
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("mission-journal-sto ready, waiting for client.startup")
