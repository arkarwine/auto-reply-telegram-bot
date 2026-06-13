# Telegram Group Interaction Bot

A Kurigram bot that interacts with eligible human messages in a group. It
chooses randomly from group-specific/global replies and sometimes adds a random
reaction.

## Ubuntu deployment

1. Create a bot with BotFather and disable its privacy mode using `/setprivacy`
   so it can receive every group message. Add the bot to the group as an
   administrator so it can reliably verify admin commands and interact with
   messages.
2. Get a Telegram API ID and API hash from <https://my.telegram.org>.
3. Install Python, then install MongoDB Community Edition using MongoDB's
   official Ubuntu instructions or use MongoDB Atlas:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv
   ```

4. Install the bot at `/opt/autoreply`:

   ```bash
   sudo useradd --system --home /opt/autoreply --shell /usr/sbin/nologin autoreply
   sudo mkdir -p /opt/autoreply
   sudo cp -a . /opt/autoreply/
   sudo chown -R autoreply:autoreply /opt/autoreply
   sudo -u autoreply python3 -m venv /opt/autoreply/.venv
   sudo -u autoreply /opt/autoreply/.venv/bin/pip install /opt/autoreply
   sudo cp deployment/autoreply.service /etc/systemd/system/
   ```

5. Copy `.env.example` to `/opt/autoreply/.env`, fill in the credentials, then
   start the services:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now autoreply
   sudo systemctl status autoreply
   ```

View logs with `sudo journalctl -u autoreply -f`.

Set your numeric Telegram user ID in `.env`:

```env
OWNER_ID=123456789
STORAGE_CHAT_ID=-1001234567890
```

`STORAGE_CHAT_ID` is optional but recommended. It should be a private channel
where the bot is an administrator. Captured replies are copied there so they
remain available even if the admin deletes the private submission. Without it,
the bot references the message submitted in private chat.

The owner can configure the `/start` and `/help` buttons in private chat:

- `/updates <url>` or `/updates off`
- `/support <url>` or `/support off`
- `/owner_link <url>` or `/owner_link off`

Calling one without a URL displays its current value. Links are persisted in
MongoDB.

The owner can attach a photo to the `/start` and `/help` menu by sending a
photo with `/start_img` as its caption, or by replying to a photo with
`/start_img`. Use `/start_img off` to remove it.

The owner can open `/global_defaults` in private chat to manage global replies.
Every enabled group rotates through its local replies and the global replies
together. Group administrators can see global replies in **View Replies**, but
cannot delete or change them.

## Private configuration

A group administrator sends `/autoreply` in the group. The bot replies with an
**Open Auto Reply Manager** button, then deletes both messages after 30 seconds.
When the bot is newly added to a group, it posts an onboarding checklist,
creates the group configuration, and enables auto-reply by default.

The private manager lets admins:

- Enable or disable interactions.
- Add any copyable Telegram message as a reply.
- View, delete, or clear replies.
- Cycle auto-reply chance between 0%, 25%, 50%, 75%, and 100%.
- Configure a group cooldown of 0, 5, 15, 30, or 60 seconds. The default is 0.
- Configure a rate limit of unlimited, 5, 10, 20, or 30 interactions per minute.
  The default is 5 per minute.
- Enable or disable reactions.
- Cycle the random reaction chance between 0%, 25%, 50%, 75%, and 100%.
- Enable or disable global replies for the group.
- Exclude individual global replies from the group.

Replies are selected randomly from the group's local replies and its allowed
global replies. Reply lists are paginated. Clearing local or global replies
requires confirmation.

Telegram flood waits are retried automatically. If Telegram reports that the
bot can no longer access or interact with a group, interactions for that group
are disabled automatically until an administrator enables them again.
If Telegram rejects a configured reaction as invalid, that reaction is removed
from the group's reaction list and auto-reply stays enabled.

Inline manager buttons use Telegram's primary, success, and danger accents to
distinguish navigation, enabling/add actions, and destructive actions.

Configuration changes happen entirely in private chat. Commands, service
events, and messages sent by other bots are ignored.

Automatic Telegram text parsing is disabled, so literal text such as
`<message>` and angle brackets inside configured replies remains visible.

After adding the bot as an administrator, send `/autoreply` in the group and
open the private manager. Use **Add Reply**, then send text, a photo, video,
sticker, document, voice note, poll, or another copyable Telegram message.
The bot registers its command menu during startup and responds to `/start` or
`/help` in private chat with setup instructions. `/start` always includes a
Help button; updates, support, and owner buttons are shown only when configured.

## Tests

```bash
python3 -m pip install -e ".[dev]"
pytest
```
