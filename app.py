import os
import re
import time
from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk.errors import SlackApiError
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

bolt_app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"]
)

flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

# --- Persistence (DB via SQLAlchemy) ---

# Prefer DATABASE_URL (e.g. Render Postgres); fall back to local SQLite file.
DEFAULT_DB_PATH = os.environ.get("PINGPONG_DB_PATH", "pingpong.db")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

engine = create_engine(DATABASE_URL, future=True)


def init_db():
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY,
                    challenger TEXT NOT NULL,
                    opponent TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id SERIAL PRIMARY KEY,
                    match_id TEXT NOT NULL,
                    challenger TEXT NOT NULL,
                    opponent TEXT NOT NULL,
                    challenger_score INTEGER NOT NULL,
                    opponent_score INTEGER NOT NULL,
                    winner TEXT,
                    channel TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    submitted_at INTEGER NOT NULL
                )
                """
            )
        )


def record_result(
    match_id: str,
    challenger: str,
    opponent: str,
    challenger_score: int,
    opponent_score: int,
    winner: str | None,
    channel: str,
    submitted_by: str,
):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO results (
                    match_id,
                    challenger,
                    opponent,
                    challenger_score,
                    opponent_score,
                    winner,
                    channel,
                    submitted_by,
                    submitted_at
                )
                VALUES (:match_id, :challenger, :opponent, :challenger_score, :opponent_score,
                        :winner, :channel, :submitted_by, :submitted_at)
                """
            ),
            {
                "match_id": match_id,
                "challenger": challenger,
                "opponent": opponent,
                "challenger_score": challenger_score,
                "opponent_score": opponent_score,
                "winner": winner,
                "channel": channel,
                "submitted_by": submitted_by,
                "submitted_at": int(time.time()),
            },
        )


def create_match(match_id: str, challenger: str, opponent: str, channel: str):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO matches (id, challenger, opponent, channel, created_at)
                VALUES (:id, :challenger, :opponent, :channel, :created_at)
                ON CONFLICT (id) DO UPDATE
                SET challenger = EXCLUDED.challenger,
                    opponent = EXCLUDED.opponent,
                    channel = EXCLUDED.channel,
                    created_at = EXCLUDED.created_at
                """
            ),
            {
                "id": match_id,
                "challenger": challenger,
                "opponent": opponent,
                "channel": channel,
                "created_at": int(time.time()),
            },
        )


def get_match(match_id: str):
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT id, challenger, opponent, channel, created_at "
                "FROM matches WHERE id = :id"
            ),
            {"id": match_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


def delete_match(match_id: str):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM matches WHERE id = :id"), {"id": match_id})


init_db()


@bolt_app.command("/pingpong")
def handle_pingpong(ack, command, respond, client):
    ack()

    text = command["text"].strip()
    user_id = command["user_id"]
    channel_id = command["channel_id"]

    if text.startswith("challenge"):
        print(f"DEBUG: Received text: '{text}'")
        # Helpful when Slack formatting doesn't match expectations
        print(f"DEBUG: Command keys: {sorted(list(command.keys()))}")
        
        opponent_id = None
        # Prefer Slack's canonical mention tokens, but some clients/contexts end up sending @U123... too.
        # Accept both:
        # - <@U123ABC>
        # - <@U123ABC|name>
        # - @U123ABC (or @W123ABC)
        match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", text)
        if match:
            opponent_id = match.group(1)
        else:
            match = re.search(r"@([UW][A-Z0-9]+)", text)
            if match:
                opponent_id = match.group(1)

        if not opponent_id:
            # If Slack didn't send a real mention token, we cannot resolve @username text.
            # Production-grade workaround: open a modal with a users_select.
            client.views_open(
                trigger_id=command["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "pingpong_pick_opponent",
                    "private_metadata": f"{channel_id}|{user_id}",
                    "title": {"type": "plain_text", "text": "Pingpong Challenge"},
                    "submit": {"type": "plain_text", "text": "Challenge"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "opponent_block",
                            "label": {"type": "plain_text", "text": "Opponent"},
                            "element": {
                                "type": "users_select",
                                "action_id": "opponent_select",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "Pick someone to challenge"
                                },
                            },
                        }
                    ],
                },
            )
            return
        
        # Create unique match ID
        match_id = f"match_{user_id}_{int(time.time())}"
        
        # Store match context in DB so it survives restarts
        create_match(match_id, user_id, opponent_id, channel_id)

        respond(
            response_type="in_channel",
            text=f"🏓 <@{user_id}> challenged <@{opponent_id}>!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<@{user_id}> has challenged <@{opponent_id}> to a ping pong match!"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Accept"},
                            "style": "primary",
                            "action_id": "accept_match",
                            "value": match_id
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Decline"},
                            "style": "danger",
                            "action_id": "decline_match",
                            "value": match_id
                        }
                    ]
                }
            ]
        )
    else:
        respond("Try `/pingpong challenge @someone`")


@bolt_app.view("pingpong_pick_opponent")
def handle_pick_opponent(ack, body, client):
    ack()

    # private_metadata: "{channel_id}|{challenger_user_id}"
    channel_id, challenger_id = body["view"]["private_metadata"].split("|", 1)
    opponent_id = body["view"]["state"]["values"]["opponent_block"]["opponent_select"]["selected_user"]

    match_id = f"match_{challenger_id}_{int(time.time())}"
    create_match(match_id, challenger_id, opponent_id, channel_id)

    # Posting can fail with `not_in_channel` if the bot isn't a member yet.
    # For public channels we can auto-join; for private channels the bot must be invited.
    try:
        try:
            client.conversations_join(channel=channel_id)
        except SlackApiError as e:
            # conversations_join only works for joinable channels; ignore failures and try posting anyway
            print(f"DEBUG: conversations_join failed: {e.response.data}")

        client.chat_postMessage(
            channel=channel_id,
            text=f"🏓 <@{challenger_id}> challenged <@{opponent_id}>!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<@{challenger_id}> has challenged <@{opponent_id}> to a ping pong match!"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Accept"},
                            "style": "primary",
                            "action_id": "accept_match",
                            "value": match_id
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Decline"},
                            "style": "danger",
                            "action_id": "decline_match",
                            "value": match_id
                        }
                    ]
                }
            ],
        )
    except SlackApiError as e:
        err = (e.response.data or {}).get("error")
        print(f"DEBUG: chat_postMessage failed: {e.response.data}")

        # Best UX: tell the user right in the modal what to do next.
        # (Avoids "nothing happened" when we can't post and can't DM.)
        try:
            if err == "not_in_channel":
                msg = (
                    "I couldn’t post the challenge because I’m not in this channel.\n\n"
                    "If this is a *private* channel, run `/invite @pingpong-bot` in the channel, then try again.\n"
                    "If it’s *public*, make sure the app has the `channels:join` scope and is reinstalled."
                )
            elif err == "missing_scope":
                msg = (
                    "I’m missing Slack permissions needed to post here.\n\n"
                    "Ask an admin to add the required scopes, then reinstall the app."
                )
            else:
                msg = f"I couldn’t post the challenge (error: `{err}`)."

            client.views_update(
                view_id=body["view"]["id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Pingpong Challenge"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"❌ {msg}"}}
                    ],
                },
            )
        except Exception as ee:
            # Last-resort fallback: try DM, but don't crash if scopes are missing.
            print(f"DEBUG: views_update failed: {ee}")
            try:
                dm = client.conversations_open(users=challenger_id)
                dm_channel = dm["channel"]["id"]
                client.chat_postMessage(channel=dm_channel, text=f"❌ {msg}")
            except Exception as eee:
                print(f"DEBUG: DM fallback failed: {eee}")
                return


@bolt_app.action("accept_match")
def accept_match(ack, body, respond):
    ack()
    match_id = body["actions"][0]["value"]

    match_data = get_match(match_id)
    if not match_data:
        respond("❌ Match not found or expired.")
        return

    respond(
        text="✅ Match accepted!",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Match accepted! <@{match_data['challenger']}> vs <@{match_data['opponent']}>. Go play 🏓"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Submit Score"},
                        "action_id": "open_score_modal",
                        "value": match_id
                    }
                ]
            }
        ]
    )


@bolt_app.action("decline_match")
def decline_match(ack, body, respond):
    ack()
    match_id = body["actions"][0]["value"]
    delete_match(match_id)
    respond("❌ Match declined.")


@bolt_app.action("open_score_modal")
def open_score_modal(ack, body, client):
    ack()
    match_id = body["actions"][0]["value"]

    # Fetch match to show clear context in the modal
    match_data = get_match(match_id)
    if not match_data:
        # Match may have expired or been deleted
        client.chat_postMessage(
            channel=body["user"]["id"],
            text="❌ Sorry, I couldn't find that match. It may have expired.",
        )
        return

    submitting_user = body["user"]["id"]
    # Determine who is "you" vs "opponent" for nicer labels
    if submitting_user == match_data["challenger"]:
        your_label = "Your score"
        opponent_label = f"Score for <@{match_data['opponent']}>"
    elif submitting_user == match_data["opponent"]:
        your_label = "Your score"
        opponent_label = f"Score for <@{match_data['challenger']}>"
    else:
        # Fallback if someone else submits the score
        your_label = "Your score"
        opponent_label = "Opponent's score"

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_score",
            "private_metadata": match_id,
            "title": {"type": "plain_text", "text": "Submit Score"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Enter the final scores for <@{match_data['challenger']}> vs <@{match_data['opponent']}>."
                    },
                },
                {
                    "type": "input",
                    "block_id": "score_block_challenger",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "score_input_challenger",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. 21"
                        }
                    },
                    "label": {
                        "type": "plain_text",
                        "text": your_label
                    }
                },
                {
                    "type": "input",
                    "block_id": "score_block_opponent",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "score_input_opponent",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. 15"
                        }
                    },
                    "label": {
                        "type": "plain_text",
                        "text": opponent_label
                    }
                }
            ]
        }
    )



@bolt_app.view("submit_score")
def handle_score_submission(ack, body, client):
    ack()  # MUST be first

    print(f"DEBUG: submit_score view payload received")

    values = body["view"]["state"]["values"]
    challenger_score_raw = values["score_block_challenger"]["score_input_challenger"]["value"]
    opponent_score_raw = values["score_block_opponent"]["score_input_opponent"]["value"]

    # Basic parsing; you can add stricter validation later
    try:
        challenger_score = int(challenger_score_raw.strip())
        opponent_score = int(opponent_score_raw.strip())
    except ValueError:
        # Re-render the modal with an error message if scores aren't integers
        ack(
            response_action="errors",
            errors={
                "score_block_challenger": "Scores must be whole numbers.",
            },
        )
        return

    match_id = body["view"]["private_metadata"]

    match_data = get_match(match_id)
    if match_data:
        channel = match_data["channel"]

        try:
            # Ensure we're in the channel before posting the result.
            try:
                client.conversations_join(channel=channel)
            except SlackApiError as e:
                print(f"DEBUG: conversations_join (score) failed: {e.response.data}")

            # Determine winner (or tie)
            winner = None
            if challenger_score > opponent_score:
                winner = match_data["challenger"]
            elif opponent_score > challenger_score:
                winner = match_data["opponent"]

            submitted_by = body["user"]["id"]
            record_result(
                match_id=match_id,
                challenger=match_data["challenger"],
                opponent=match_data["opponent"],
                challenger_score=challenger_score,
                opponent_score=opponent_score,
                winner=winner,
                channel=channel,
                submitted_by=submitted_by,
            )

            winner_line = ""
            if winner:
                winner_line = f"\n*Winner:* <@{winner}>"
            else:
                winner_line = "\n*Result:* Tie game"

            client.chat_postMessage(
                channel=channel,
                text=(
                    f"🏓 Match Result: <@{match_data['challenger']}> vs <@{match_data['opponent']}>\n"
                    f"*Scores:* <@{match_data['challenger']}> {challenger_score} – "
                    f"<@{match_data['opponent']}> {opponent_score}"
                    f"{winner_line}"
                )
            )
            delete_match(match_id)
        except SlackApiError as e:
            err = (e.response.data or {}).get("error")
            print(f"DEBUG: chat_postMessage (score) failed: {e.response.data}")

            # Fallback: DM the submitting user so the score is still visible somewhere.
            user_id = body["user"]["id"]
            try:
                dm = client.conversations_open(users=user_id)
                dm_channel = dm["channel"]["id"]
                client.chat_postMessage(
                    channel=dm_channel,
                    text=(
                        "🏓 I couldn't post the match result to the channel "
                        f"(error: `{err}`), but your scores were recorded as "
                        f"{challenger_score}-{opponent_score} for match `{match_id}`."
                    )
                )
            except Exception as eee:
                print(f"DEBUG: DM fallback (score) failed: {eee}")
    else:
        user_id = body["user"]["id"]
        client.chat_postMessage(
            channel=user_id,
            text="🏓 Score recorded (match context lost)."
        )


@flask_app.route("/slack/commands", methods=["POST"])
def slack_commands():
    return handler.handle(request)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


if __name__ == "__main__":
    flask_app.run(port=3000, debug=True)
