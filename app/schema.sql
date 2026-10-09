CREATE TABLE IF NOT EXISTS teams (
    id          INT PRIMARY KEY,
    name        VARCHAR(50),
    short_name  VARCHAR(10),
    strength    INT
);

CREATE TABLE IF NOT EXISTS players (
    id                          INT PRIMARY KEY,
    web_name                    VARCHAR(60),
    first_name                  VARCHAR(60),
    second_name                 VARCHAR(60),
    team                        INT,
    position                    VARCHAR(3),
    price                       DECIMAL(4,1),
    total_points                INT,
    form                        DECIMAL(4,1),
    points_per_game             DECIMAL(4,1),
    selected_by_percent         DECIMAL(5,1),
    minutes                     INT,
    goals_scored                INT,
    assists                     INT,
    clean_sheets                INT,
    expected_goals              DECIMAL(6,2),
    expected_assists            DECIMAL(6,2),
    expected_goal_involvements  DECIMAL(6,2),
    ict_index                   DECIMAL(6,1),
    status                      CHAR(1),
    chance_of_playing_next_round INT NULL,
    news                        VARCHAR(255),
    INDEX idx_position (position),
    INDEX idx_team (team)
);

CREATE TABLE IF NOT EXISTS events (
    id                  INT PRIMARY KEY,
    name                VARCHAR(30),
    deadline_time       DATETIME,
    finished            BOOLEAN,
    is_current          BOOLEAN,
    is_next             BOOLEAN,
    average_entry_score INT,
    highest_score       INT NULL
);

CREATE TABLE IF NOT EXISTS fixtures (
    id                  INT PRIMARY KEY,
    event               INT NULL,
    team_h              INT,
    team_a              INT,
    kickoff_time        DATETIME NULL,
    team_h_difficulty   INT,
    team_a_difficulty   INT,
    team_h_score        INT NULL,
    team_a_score        INT NULL,
    finished            BOOLEAN,
    INDEX idx_event (event)
);