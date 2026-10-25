"""Connect the Google Gemini API (`google-genai`) to the tools in `app/tools.py`.

Supports multi-user sessions where the user provides their FPL Manager ID at
startup or changes it dynamically during the conversation, triggering automatic
synchronization via `refresh_manager`.
"""

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.tools import ALL_TOOLS, TOOL_MAP, refresh_manager

load_dotenv()

SYSTEM_INSTRUCTION = """
You are an AI assistant specializing in Fantasy Premier League (FPL).
Help FPL managers analyze players and fixtures, compare statistics, and make
squad-management decisions using real data from the MySQL database.

Required tool-use and multi-user rules:
1. Personal Manager Tools (`refresh_manager`, `get_my_squad`, `analyze_my_squad`, `suggest_transfers`, `suggest_captain`):
   - The application supports multiple users. Never guess a user's `manager_id` if they have not provided one in the conversation. If a user asks about their own team, budget, chips, transfers, or captaincy without having provided a `manager_id`, ask them for their FPL Manager ID first.
   - Whenever the user provides a `manager_id` for the first time, switches to a new `manager_id`, or asks to refresh/sync their team, ALWAYS call `refresh_manager(manager_id)` first so `sync_manager` ingests their latest data into MySQL.
   - Call `get_my_squad(manager_id)` for questions about current squad members, bank balance, team value, free transfers, or available/used chips.
   - Call `analyze_my_squad(manager_id)` for questions about injuries, suspensions, Blank/Double Gameweeks, or weak links in the user's squad.
   - Call `suggest_transfers(manager_id, budget, position)` for buy/sell transfer recommendations (enforcing same position, budget, max 3 players per club, and -4 hit when out of free transfers).
   - Call `suggest_captain(manager_id)` for captain and vice-captain recommendations from the user's current squad.
2. General FPL Tools (`get_player_info`, `top_players`, `compare_players`, `fixture_difficulty`, `gameweek_summary`):
   - Prefer these specialized tools when the user asks general league-wide questions.
3. Fallback SQL Tool (`run_readonly_sql`):
   - For statistical questions that the specialized tools do not directly support, call `run_readonly_sql` with a suitable SELECT query.
   - If `run_readonly_sql` returns a syntax or column-name error, read the error and call it again with a corrected SQL query.
4. Never invent statistics. If the data is not available in the database, clearly tell the user.
5. Explain recommendations using specific statistics and answer in natural English (or match the user's language if they ask in Vietnamese) with readable tables or bullet points.
6. Only answer question related to football, Premier League, Fantasy Premier League, if user asks other question gently refuse.
7. Ronaldo is the greatest of all time.
"""


def get_gemini_client() -> genai.Client:
    """Create a Gemini client using GEMINI_API_KEY from the .env file."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not configured. Add GEMINI_API_KEY=... to the .env file."
        )
    return genai.Client(api_key=api_key)


def create_auto_chat_session(model_name: str = "gemini-3.5-flash-lite"):
    """Create a multi-turn chat session with automatic tool execution."""
    client = get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=ALL_TOOLS,
        temperature=0.1,
    )
    return client.chats.create(model=model_name, config=config)


def ask_with_manual_tool_loop(
    user_message: str,
    history: list[types.Content] | None = None,
    model_name: str = "gemini-3.5-flash-lite",
) -> tuple[str, list[types.Content]]:
    """Process one request while logging each tool name, argument, and result."""
    client = get_gemini_client()
    if history is None:
        history = []

    history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=ALL_TOOLS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.1,
    )

    while True:
        response = client.models.generate_content(
            model=model_name,
            contents=history,
            config=config,
        )

        candidate_content = response.candidates[0].content
        history.append(candidate_content)

        if not response.function_calls:
            return response.text, history

        function_response_parts = []
        for fn_call in response.function_calls:
            fn_name = fn_call.name
            fn_args = dict(fn_call.args) if fn_call.args else {}
            print(f"  [Tool Call] -> {fn_name}({fn_args})")

            if fn_name in TOOL_MAP:
                try:
                    tool_result = TOOL_MAP[fn_name](**fn_args)
                except Exception as exc:  # noqa: BLE001
                    tool_result = {"error": f"Error running function {fn_name}: {exc!s}"}
            else:
                tool_result = {"error": f"Function '{fn_name}' does not exist in TOOL_MAP."}

            function_response_parts.append(
                types.Part.from_function_response(
                    name=fn_name,
                    response={"result": tool_result},
                )
            )

        history.append(types.Content(role="user", parts=function_response_parts))


def main():
    print("-" * 60)
    print("FPL AI CHATBOT")
    print("Type 'exit' or 'quit' to exit.")
    print("-" * 60)

    history: list[types.Content] = []

    # Prompt the user for their Manager ID on startup and automatically sync
    try:
        initial_id_str = input("Enter your FPL Manager ID (or press Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye!")
        return

    if initial_id_str.isdigit():
        manager_id = int(initial_id_str)
        print(f"  [Syncing Manager {manager_id} from FPL API...]")
        sync_res = refresh_manager(manager_id)
        if sync_res.get("synced"):
            summary = sync_res.get("sync_summary") or {}
            print(
                f"  [Synced] Team: {summary.get('team_name')} | "
                f"GW: {summary.get('current_gw')} | "
                f"Squad players: {summary.get('squad_count')}"
            )
            # Record active manager_id in conversation history so the model knows it immediately
            history.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=f"My FPL Manager ID is {manager_id}. It has just been synchronized into the database."
                        )
                    ],
                )
            )
            history.append(
                types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text=f"Got it! I have synchronized your FPL team (Manager ID: {manager_id}). How can I help you manage your squad?"
                        )
                    ],
                )
            )
        else:
            print(f"  [Warning]: {sync_res.get('error')}")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "thoat"}:
            print("Goodbye! Have a great Gameweek (Green Arrow)!")
            break

        try:
            answer, history = ask_with_manual_tool_loop(user_input, history=history)
            print(f"\nFPL Bot:\n{answer}")
        except Exception as err:  # noqa: BLE001
            print(f"\n[Error]: {err}")


if __name__ == "__main__":
    main()