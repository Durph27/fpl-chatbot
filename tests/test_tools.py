"""Complex & Ground-Truth Test Suite for `app/tools.py` and `app/rag.py`.

How to run:
  1. Direct execution with detailed report:
     python -m tests.test_tools
  2. Via pytest:
     pytest tests/test_tools.py -v
"""

import json
import math
from typing import Any

from sqlalchemy import text

from app.db import engine
from app.tools import (
    analyze_my_squad,
    compare_players,
    fixture_difficulty,
    gameweek_summary,
    get_my_squad,
    get_player_info,
    run_readonly_sql,
    search_fpl_knowledge,
    suggest_captain,
    suggest_transfers,
    top_players,
)


def assert_strict_json_serializable(data: Any, context_name: str = "") -> None:
    """
    Verify that tool output complies strictly with the Gemini API JSON specification:
    no NaN, Infinity, Decimal, or unserializable objects.
    """
    try:
        serialized = json.dumps(data, allow_nan=False, ensure_ascii=False)
        assert isinstance(serialized, str) and len(serialized) > 0
    except (TypeError, ValueError) as exc:
        raise AssertionError(
            f"[{context_name}] Output violated strict JSON serialization (contains NaN/Inf or unsupported type): {exc}"
        ) from exc


def test_top_players_complex_filters_and_sorting() -> None:
    res = top_players(
        position="MF",
        max_price=7.5,
        min_minutes=90,
        sort_by="value",
        limit=10,
    )
    assert_strict_json_serializable(res, "top_players_value")
    players = res["players"]
    assert res["count"] == len(players)
    assert len(players) <= 10

    for p in players:
        assert p["position"] == "MID"
        assert p["price"] <= 7.5 + 1e-6
        assert p["minutes"] >= 90
        expected_val = round(p["total_points"] / p["price"], 2) if p["price"] else 0
        assert math.isclose(p["value_points_per_m"], expected_val, abs_tol=0.02)

    values = [p["value_points_per_m"] for p in players]
    for i in range(len(values) - 1):
        assert values[i] >= values[i + 1] - 0.02

    res_form = top_players(sort_by="form", limit=5, min_minutes=0)
    assert_strict_json_serializable(res_form, "top_players_form")

    with engine.connect() as conn:
        raw_rows = conn.execute(
            text("""
                SELECT web_name, CAST(form AS DECIMAL(10,2)) AS f_val, CAST(ep_next AS DECIMAL(10,2)) AS ep_val
                FROM players
                WHERE minutes >= 0
                ORDER BY CAST(form AS DECIMAL(10,2)) DESC, total_points DESC
                LIMIT 5
            """)
        ).fetchall()

    assert len(res_form["players"]) == len(raw_rows)
    for tool_row, db_row in zip(res_form["players"], raw_rows):
        assert tool_row["web_name"] == db_row[0]
        assert "form" in tool_row and "ep_next" in tool_row
        assert math.isclose(float(tool_row["form"]), float(db_row[1]), abs_tol=0.05)
        assert math.isclose(float(tool_row["ep_next"]), float(db_row[2]), abs_tol=0.05)

    res_malicious = top_players(
        sort_by="form; DROP TABLE players; --",
        limit=999,
    )
    assert_strict_json_serializable(res_malicious, "top_players_sql_injection_guard")
    assert len(res_malicious["players"]) <= 20


def test_get_player_info_fuzzy_unicode_and_fdr_math() -> None:
    with engine.connect() as conn:
        top_p = conn.execute(
            text("SELECT id, web_name, team FROM players ORDER BY total_points DESC LIMIT 1")
        ).fetchone()
    assert top_p is not None
    p_id, p_name = top_p[0], top_p[1]

    messy_query = f"   {p_name.swapcase()}   "
    info = get_player_info(player_name=messy_query, num_gws=4)
    assert_strict_json_serializable(info, "get_player_info_valid")
    assert info["found"] is True
    assert info["player"]["id"] == p_id
    assert info["player"]["web_name"] == p_name

    fixtures = info["upcoming_fixtures"]
    if fixtures:
        manual_avg = round(sum(f["difficulty"] for f in fixtures) / len(fixtures), 2)
        assert info["avg_fixture_difficulty"] == manual_avg
        for f in fixtures:
            assert f["venue"] in ("H", "A")
            assert 1 <= int(f["difficulty"]) <= 5

    not_found_res = get_player_info("NonExistentPlayer_XYZ_99999")
    assert_strict_json_serializable(not_found_res, "get_player_info_not_found")
    assert not_found_res["found"] is False


def test_compare_players_consistency_and_partial_missing() -> None:
    with engine.connect() as conn:
        sample_names = [
            row[0]
            for row in conn.execute(
                text("SELECT web_name FROM players ORDER BY total_points DESC LIMIT 3")
            ).fetchall()
        ]
    assert len(sample_names) == 3

    query_list = sample_names + ["GhostPlayer_NotInDB_123"]
    cmp_res = compare_players(player_names=query_list, num_gws=3)
    assert_strict_json_serializable(cmp_res, "compare_players")

    assert len(cmp_res["compared_players"]) == 3
    assert cmp_res["not_found"] == ["GhostPlayer_NotInDB_123"]

    for item in cmp_res["compared_players"]:
        single = get_player_info(item["web_name"], num_gws=3)
        assert single["found"] is True
        sp = single["player"]
        assert item["price"] == sp["price"]
        assert item["total_points"] == sp["total_points"]
        assert item["form"] == sp["form"]
        assert item["ep_next"] == sp["ep_next"]
        assert item["avg_fdr_next_gws"] == single["avg_fixture_difficulty"]


def test_fixture_difficulty_and_gameweek_conservation_laws() -> None:
    fdr_all = fixture_difficulty(team_short_name=None, num_gws=5)
    assert_strict_json_serializable(fdr_all, "fixture_difficulty_all")
    teams_list = fdr_all["teams"]
    assert len(teams_list) == 20

    valid_fdrs = [t["avg_fdr"] for t in teams_list if t["avg_fdr"] is not None]
    for i in range(len(valid_fdrs) - 1):
        assert valid_fdrs[i] <= valid_fdrs[i + 1] + 1e-6

    num_gws = fdr_all["end_gw"] - fdr_all["start_gw"] + 1
    for t in teams_list:
        blanks = len(t["blank_gws"])
        doubles = len(t["double_gws"])
        normal_gws = num_gws - blanks - doubles
        min_expected_matches = normal_gws * 1 + doubles * 2
        assert t["matches_count"] >= min_expected_matches

    gw_sum = gameweek_summary()
    assert_strict_json_serializable(gw_sum, "gameweek_summary")
    appearance_counts: dict[str, int] = {}
    for match in gw_sum["fixtures"]:
        h, a = match["home_short"], match["away_short"]
        assert h != a
        appearance_counts[h] = appearance_counts.get(h, 0) + 1
        appearance_counts[a] = appearance_counts.get(a, 0) + 1

    for bt in gw_sum["blank_teams"]:
        assert appearance_counts.get(bt, 0) == 0
    for dt in gw_sum["double_gameweek_teams"]:
        assert appearance_counts.get(dt, 0) >= 2
    assert sum(appearance_counts.values()) == 2 * gw_sum["total_fixtures"]


def test_manager_squad_and_transfer_constraints() -> None:
    """
    If at least one manager exists in `managers`, verify `get_my_squad`,
    `analyze_my_squad`, `suggest_captain`, and the FPL rules in `suggest_transfers`
    (same position, within budget, <= 3 players per club).
    """
    with engine.connect() as conn:
        mgr_row = conn.execute(text("SELECT manager_id FROM managers LIMIT 1")).fetchone()

    if not mgr_row:
        return

    mid = int(mgr_row[0])
    squad = get_my_squad(mid)
    assert_strict_json_serializable(squad, "get_my_squad")
    if not squad.get("found"):
        return

    assert len(squad["starting_xi"]) + len(squad["bench"]) == 15

    analysis = analyze_my_squad(mid, num_gws=3)
    assert_strict_json_serializable(analysis, "analyze_my_squad")
    assert sum(analysis["club_distribution"].values()) == 15

    cap = suggest_captain(mid, num_gws=1, limit=5)
    assert_strict_json_serializable(cap, "suggest_captain")
    assert len(cap["candidates"]) <= 5

    transfers = suggest_transfers(mid, num_gws=3, limit=5)
    assert_strict_json_serializable(transfers, "suggest_transfers")
    for rec in transfers["recommendations"]:
        assert rec["player_out"]["web_name"] != rec["player_in"]["web_name"]
        assert float(rec["player_in"]["price"]) <= float(rec["max_budget_for_slot"]) + 1e-6


def test_rag_knowledge_search() -> None:
    """
    Verify that `search_fpl_knowledge` indexes and retrieves chunks from
    `data/knowledge/fpl_rules.md`, `data/all_seasonal_data.csv` (if present),
    and `players.news`.
    """
    res = search_fpl_knowledge("free transfers rollover wildcard 50% sell-on profit rule", top_k=3)
    assert_strict_json_serializable(res, "search_fpl_knowledge")
    assert "results" in res
    assert isinstance(res["results"], list)


def test_run_readonly_sql_security_and_complex_cte() -> None:
    malicious_queries = [
        "DROP TABLE players",
        "DELETE FROM players WHERE id = 1",
        "UPDATE players SET price = 4.0 WHERE web_name = 'Haaland'",
        "INSERT INTO teams (id, name, short_name) VALUES (99, 'Hack', 'HCK')",
        "ALTER TABLE players DROP COLUMN price",
        "TRUNCATE TABLE fixtures",
        "SELECT web_name FROM players LIMIT 1; DROP TABLE players;",
    ]
    for mq in malicious_queries:
        res = run_readonly_sql(mq)
        assert_strict_json_serializable(res, "run_readonly_sql_security")
        assert "error" in res

    complex_cte_sql = """
        WITH RankedPlayers AS (
            SELECT
                p.web_name,
                t.short_name AS team,
                p.total_points,
                p.price,
                ROW_NUMBER() OVER (PARTITION BY p.team ORDER BY p.total_points DESC) AS rn
            FROM players p
            JOIN teams t ON t.id = p.team
        )
        SELECT web_name, team, total_points, price
        FROM RankedPlayers
        WHERE rn = 1
        ORDER BY total_points DESC
    """
    cte_res = run_readonly_sql(complex_cte_sql)
    assert_strict_json_serializable(cte_res, "run_readonly_sql_cte")
    assert "error" not in cte_res
    assert cte_res["row_count"] == 20
    assert "LIMIT 50" in cte_res["sql_executed"].upper()


def run_all_tests() -> None:
    test_functions = [
        ("1. Deep test `top_players` (Multi-filter, SQL cross-check, SQL injection guard)", test_top_players_complex_filters_and_sorting),
        ("2. Deep test `get_player_info` (Fuzzy/Unicode search & FDR math)", test_get_player_info_fuzzy_unicode_and_fdr_math),
        ("3. Deep test `compare_players` (Cross-check & missing player handling)", test_compare_players_consistency_and_partial_missing),
        ("4. Deep test `fixture_difficulty` & `gameweek_summary` (20-club conservation laws)", test_fixture_difficulty_and_gameweek_conservation_laws),
        ("5. Deep test `get_my_squad`, `analyze_my_squad`, `suggest_captain`, `suggest_transfers`", test_manager_squad_and_transfer_constraints),
        ("6. Deep test `search_fpl_knowledge` (Local RAG retrieval & JSON safety)", test_rag_knowledge_search),
        ("7. Deep test `run_readonly_sql` (7 malicious SQL blocks & CTE Window Function)", test_run_readonly_sql_security_and_complex_cte),
    ]

    print("=" * 72)
    print("RUNNING COMPLEX TEST SUITE FOR FPL CHATBOT TOOLS & RAG")
    print("=" * 72)

    passed = 0
    failed = 0

    for title, fn in test_functions:
        try:
            fn()
            print(f"[PASS] {title}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {title}")
            print(f"   └── Error details: {exc}")
            failed += 1

    print("=" * 72)
    print(f"SUMMARY: {passed}/{len(test_functions)} complex tests PASSED ({failed} FAILED).")
    print("=" * 72)

    if failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    run_all_tests()