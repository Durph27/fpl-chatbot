# Official Fantasy Premier League (FPL) Rules, Scoring, Chips, and Strategy Guide

## 1. Squad Size, Budget, and Formation Rules
- **Squad Size**: Every FPL squad must consist of exactly **15 players**:
  - 2 Goalkeepers (`GK`)
  - 5 Defenders (`DEF`)
  - 5 Midfielders (`MID`)
  - 3 Forwards (`FWD`)
- **Starting Budget**: At the beginning of the season, every manager receives a starting budget of **£100.0m**.
- **Club Limit (3-Player Rule)**: You can own a maximum of **3 players** from any single Premier League club (`teams`). If a real-life transfer causes you to own 4 players from the same club, you must sell one of those players when making your next transfer.
- **Starting XI and Valid Formations**: Each Gameweek, you select 11 starters from your 15-man squad before the Gameweek deadline. Your Starting XI must always include **1 Goalkeeper**, at least **3 Defenders**, at least **2 Midfielders**, and at least **1 Forward** (valid formations: 3-4-3, 3-5-2, 4-3-3, 4-4-2, 4-5-1, 5-3-2, 5-4-1, 5-2-3).

## 2. Gameweek Deadlines, Captaincy, and Automatic Substitutions (Auto-Subs)
- **Gameweek Deadline**: All line-up changes, captain selections, transfers, and chip activations for a Gameweek must be confirmed **90 minutes before the kickoff of the first match** of that Gameweek (`events.deadline_time`).
- **Captain and Vice-Captain**:
  - Your **Captain** (`is_captain = 1`) has their Gameweek score multiplied by **2** (`multiplier = 2`), or multiplied by **3** (`multiplier = 3`) if the **Triple Captain** chip is active.
  - If your Captain plays **0 minutes** in the Gameweek, the captaincy multiplier automatically transfers to your **Vice-Captain** (`is_vice_captain = 1`).
  - If both your Captain and Vice-Captain play 0 minutes, no player receives the double-points bonus.
- **Automatic Substitutions (Auto-Subs)**:
  - Your 4 bench players (`squad_position` 12, 13, 14, 15) are ordered by priority: position 12 is your reserve Goalkeeper, while positions 13, 14, and 15 are your 1st, 2nd, and 3rd outfield substitutes.
  - If your starting Goalkeeper plays 0 minutes, your reserve Goalkeeper (position 12) is subbed in automatically if they played at least 1 minute.
  - If any starting outfield player plays 0 minutes, they are automatically replaced by the highest-priority outfield bench player who played at least 1 minute **and** does not break the minimum formation rules (at least 3 DEF, 2 MID, 1 FWD).

## 3. FPL Scoring System (Points Breakdown)
- **Minutes Played**:
  - Playing 1 to 59 minutes: **+1 point**
  - Playing 60 minutes or more (excluding stoppage time): **+2 points**
- **Goals Scored**:
  - Goalkeeper (`GK`) or Defender (`DEF`): **+6 points** per goal
  - Midfielder (`MID`): **+5 points** per goal
  - Forward (`FWD`): **+4 points** per goal
- **Assists**: **+3 points** for any position (awarded to the player from the scoring team who makes the final pass, cross, or shot/rebound leading to a goal).
- **Clean Sheets** (playing at least 60 minutes while the team concedes 0 goals during the time the player is on the pitch):
  - Goalkeeper (`GK`) or Defender (`DEF`): **+4 points**
  - Midfielder (`MID`): **+1 point**
  - Forward (`FWD`): **0 points**
- **Goalkeeper Saves and Penalties**:
  - Every 3 shots saved by a Goalkeeper: **+1 point**
  - Penalty save: **+5 points**
  - Penalty miss (any position): **-2 points**
- **Goals Conceded & Cards /Fouls**:
  - Every 2 goals conceded by a `GK` or `DEF`: **-1 point**
  - Yellow card: **-1 point**
  - Red card: **-3 points** (includes any yellow card points; if a player receives a red card, they continue to be penalized for goals conceded by their team after they leave the pitch)
  - Own goal: **-2 points**
- **Bonus Points (BPS - Bonus Points System)**:
  - Based on Opta statistics (`bps`), the top 3 performing players in each match receive **3, 2, and 1 Bonus Points** (`bonus`).
  - Tie-breaker rules: If two players tie for 1st place in BPS, both get 3 points and the next player gets 1 point (3, 3, 1). If two players tie for 2nd place, the top player gets 3 and both tied players get 2 (3, 2, 2). If two players tie for 3rd place, both get 1 point (3, 2, 1, 1).
- **Defensive Contribution (`defensive_contribution`) & New Metrics**:
  - In newer FPL seasons and datasets (`all_seasonal_data.csv`), defensive actions such as clearances, blocks, interceptions (`clearances_blocks_interceptions`), tackles, and recoveries (`recoveries`) feed into the Bonus Points System (`bps`) and the `defensive_contribution` metric, rewarding centre-backs and defensive midfielders even when they do not register goals or assists.

## 4. Transfers, Rolling Free Transfers (Up to 5), and Player Price Changes
- **Free Transfers and Banking Up to 5 Free Transfers**:
  - After GW1, you receive **1 Free Transfer** per Gameweek.
  - Under the updated FPL rules, unused Free Transfers roll over and can be banked up to a maximum of **5 Free Transfers** (`managers.free_transfers`).
  - **Preserved Transfers When Using Chips**: When you activate the **Wildcard** or **Free Hit** chip, any banked Free Transfers (up to 5) are **preserved** for the following Gameweek instead of resetting to 1!
- **Point Hits (-4 Points)**:
  - Each extra transfer made beyond your available `free_transfers` in a Gameweek costs **-4 points** deducted from your score at the next Gameweek deadline (unless Wildcard or Free Hit is active).
- **Player Price Changes and the 50% Sell-On Profit Rule**:
  - Player prices (`price`) rise or fall by **£0.1m** per day (up to a maximum of **±£0.3m** within a single Gameweek) based on net transfer volume (`transfers_in_event` vs `transfers_out_event`).
  - **50% Sell-On Rule**: When you sell a player whose current price is higher than the price you bought them for, you only keep **50% of the profit, rounded down to the nearest £0.1m**. For example, if you bought a player at £7.0m and their price rises to £7.3m, your selling price is £7.1m (+£0.1m profit). If they rise to £7.4m, your selling price is £7.2m (+£0.2m profit). If a player's price drops below your purchase price, you lose the full drop amount.

## 5. FPL Chips and Assistant Manager Rules
- **Wildcard (`wildcard`)**: Allows unlimited permanent transfers in a single Gameweek for 0 points cost. You get two Wildcards per season (first half and second half of the season).
- **Free Hit (`freehit`)**: Allows unlimited temporary transfers for **one Gameweek only**. After the Gameweek finishes, your squad reverts to how it was before the Free Hit was played. Ideal for major **Blank Gameweeks** (when many clubs have 0 fixtures) or **Double Gameweeks**.
- **Bench Boost (`bboost`)**: Counts the points scored by all 4 of your bench players (`squad_position` 12–15) toward your total Gameweek score in addition to your Starting XI. Best used in a **Double Gameweek** when all 15 squad members have fixtures.
- **Triple Captain (`3xc`)**: Multiplies your Captain's points by **3** (`multiplier = 3`) instead of 2. Best used on a premium attacker (`Haaland`, `Salah`, `Palmer`, `Saka`) who has a **Double Gameweek** (2 matches in one Gameweek) or a very easy home fixture (`FDR = 1` or `2`).
- **Assistant Manager Chip (`manager` / `mng_*` columns in `all_seasonal_data.csv`)**:
  - Activated for 3 consecutive Gameweeks using your bank balance.
  - Earns points based on the real-life club's result: Win (`mng_win`: +6 pts), Draw (`mng_draw`: +3 pts), Loss (`mng_loss`: 0 pts), Goals Scored (`mng_goals_scored`: +1 pt per goal), Clean Sheet (`mng_clean_sheets`: +2 pts).
  - **Table Bonus (Underdog Bonus)**: If your chosen club beats or draws against an opponent ranked at least **5 places higher** in the Premier League table at the start of the Gameweek, you earn an extra Underdog Win bonus (`mng_underdog_win`: +10 pts, total 16 pts for the win) or Underdog Draw bonus (`mng_underdog_draw`: +5 pts, total 8 pts for the draw).
- **One Chip Per Gameweek**: Only one single-Gameweek chip (`Wildcard`, `Free Hit`, `Bench Boost`, `Triple Captain`) can be active in the same Gameweek.

## 6. Key FPL Strategy & Analytics Concepts (`ep_next`, `FDR`, `xG`, `xA`, `xGI`, `ICT`)
- **FDR (Fixture Difficulty Rating)**: Official rating from **1 (easiest)** to **5 (hardest)**. Target players whose clubs have an average FDR below `2.8` over the next 3–5 Gameweeks.
- **Blank Gameweek (BGW) & Double Gameweek (DGW)**:
  - **Blank Gameweek**: A club has **0 matches** in that Gameweek (due to FA Cup/EFL Cup postponements). Players from that club score 0 points.
  - **Double Gameweek**: A club plays **2 matches** in a single Gameweek. Players earn points from both games, making them prime targets for transfers and captaincy.
- **Expected Stats (`xG`, `xA`, `xGI`, `xGC`)**:
  - `xG` (Expected Goals) and `xA` (Expected Assists) measure the quality of scoring and playmaking chances. `xGI = xG + xA` (Expected Goal Involvements).
  - A player with high `xGI` per 90 minutes who has recently blanked is "underperforming their xGI" and is statistically likely to haul soon if their upcoming fixtures (`FDR`) are favorable.
- **`ep_next` (Expected Points Next Round) & `form`**:
  - `form` is a player's average points per match calculated over all matches played by their club in the **last 30 days**.
  - `ep_next` combines `form`, upcoming fixture difficulty (`FDR`), home/away status, and injury probability (`chance_of_playing_next_round`).
- **When to Take a -4 Point Hit**:
  - Avoid taking a `-4` hit purely for a luxury sideways transfer.
  - Taking a `-4` hit is justified when: (1) you cannot field 11 starting players due to injuries/suspensions/Blank Gameweeks (since the new player gets +2 appearance points immediately, reducing the effective hit to -2), or (2) you are bringing in a Double Gameweek captain or a high-`ep_next` player whose projected score over the next 2–3 Gameweeks exceeds the outgoing player by more than 4 points.
