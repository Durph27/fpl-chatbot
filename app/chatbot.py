"""Connect the Google Gemini API (`google-genai`) to the tools in `app/tools.py`.

Supports automatic function calling and a manual function-calling loop for
detailed logging and debugging.
"""

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.tools import ALL_TOOLS, TOOL_MAP

load_dotenv()

SYSTEM_INSTRUCTION = """
You are an AI assistant specializing in Fantasy Premier League (FPL).
Help FPL managers analyze players and fixtures, compare statistics, and make
squad-management decisions using real data from the MySQL database.

Required tool-use rules:
1. Prefer the five specialized tools (`get_player_info`, `top_players`, `compare_players`, `fixture_difficulty`, and `gameweek_summary`) when a question matches their capabilities.
2. For statistical questions that the specialized tools do not directly support (for example, the team with the most clean sheets, the most away goals, or the most yellow cards), call `run_readonly_sql` with a suitable SELECT query against the `fixtures`, `teams`, `players`, `events`, or `player_history` tables.
3. If `run_readonly_sql` returns a syntax or column-name error, read the error and call it again with a corrected SQL query.
4. Never invent statistics. If the data is not available in the database, clearly tell the user.
5. Explain recommendations using specific statistics and answer in natural English with readable tables or bullet points.
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