"""Deterministic aggregation of the project's historical CSV."""
import re
import unicodedata

from app.rag import _load_seasonal_df


def normalize_name(value: str) -> str:
    value = unicodedata.normalize('NFKD', value.replace('đ', 'd').replace('Đ', 'D'))
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z]+', value.lower()))


def historical_gameweeks(season: str = '', player_name: str = '', gameweek: int = 0,
                        order: str = 'lowest', limit: int = 10) -> dict:
    """Query historical CSV only. Sum match points per player/Gameweek, including double GWs.

    season: YYYY-YY or YYYY/YYYY; empty searches all available seasons.
    player_name: Full name or surname; empty includes every player.
    gameweek: Specific GW, or 0 for all GWs.
    order: lowest or highest, ranked by total Gameweek points.
    limit: Maximum ranked groups (1-50), including ties at cutoff.
    """
    df = _load_seasonal_df().copy()
    available = sorted(df['season'].unique().tolist())
    if season:
        match = re.fullmatch(r'(20\d{2})[-/–](\d{2}|\d{4})', season.strip())
        if not match:
            return {'error': 'Use YYYY-YY for season.'}
        season = match[1] + '-' + match[2][-2:]
        df = df[df.season == season]
    if player_name:
        needle = normalize_name(player_name)
        names = df.name.map(normalize_name)
        mask = names.map(lambda name: bool(needle) and f' {needle} ' in f' {name} ').astype(bool)
        df = df.loc[mask]
    if gameweek:
        df = df[df['_gw'] == gameweek]
    if order not in ('lowest', 'highest'):
        return {'error': 'order must be lowest or highest'}
    if df.empty:
        return {'source': 'data/all_seasonal_data.csv', 'available_seasons': available,
                'season': season, 'found': False, 'results': []}
    keys = ['season', 'element', 'name', '_gw']
    grouped = df.groupby(keys, dropna=False).total_points.sum()
    ranked = grouped.sort_values(ascending=order == 'lowest')
    cutoff = ranked.iloc[min(max(1, min(int(limit), 50)), len(ranked)) - 1]
    ranked = ranked[ranked <= cutoff] if order == 'lowest' else ranked[ranked >= cutoff]
    results = []
    for (s, element, name, gw), points in ranked.items():
        rows = df[(df.season == s) & (df.element == element) & (df['_gw'] == gw)]
        fields = ['total_points', 'minutes', 'assists', 'goals_scored', 'bonus', 'bps',
                  'expected_goal_involvements', 'own_goals', 'red_cards', 'goals_conceded']
        results.append({'season': s, 'player': name, 'gameweek': int(gw),
                        'total_points': int(points), 'matches': rows[fields].astype(object).where(rows[fields].notna(), None).to_dict('records')})
    return {'source': 'data/all_seasonal_data.csv', 'season': season,
            'available_seasons': available, 'found': True, 'results': results}
