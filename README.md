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
SUDOER_IDS=111111111,222222222
STORAGE_CHAT_ID=-1001234567890
```

`OWNER_ID` is the primary bot owner. `SUDOER_IDS` is optional and accepts
comma- or space-separated Telegram user IDs. Owner and sudoers can configure
global defaults, start-menu links, and the start image.

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

New global replies become available immediately in every enabled group with
global replies turned on, including groups without a saved local reply.
The Global Defaults manager also configures the initial enabled state, reply
chance, reaction settings, cooldown, and rate limit inherited live by groups.
Global changes take effect immediately unless a group locally overrides that
specific setting. Each behavior button cycles through local values and then a
**Global** option that restores live inheritance for that setting.

Owner and sudoers can use `/broadcast <text>` in private chat, or reply to any
Telegram message with `/broadcast`, then confirm delivery to every known group.
Broadcasts retry flood waits and report sent/failed totals. Delivery pauses for
3 seconds after every batch of 20 groups.

## Private configuration

A group administrator sends `/autoreply` in the group. The bot replies with an
**Open Auto Reply Manager** button, then deletes both messages after 30 seconds.
When the bot is newly added to a group, it posts an onboarding checklist,
creates the group configuration, and enables auto-reply by default.

The private manager lets admins:

- Enable or disable interactions.
- Add any copyable Telegram message as a reply.
- View, delete, or clear replies.
- Cycle auto-reply chance between 0%, 25%, 50%, 75%, and 100%. The default is
  50%.
- Configure a group cooldown of 0, 5, 10, 15, 30, or 60 seconds. The default
  is 10 seconds.
- Configure a rate limit of unlimited, 5, 10, 20, or 30 interactions per minute.
  The default is unlimited.
- Enable or disable reactions.
- Cycle the random reaction chance between 0%, 25%, 50%, 75%, and 100%.
- Enable or disable global replies for the group.
- Exclude individual global replies from the group.
Replies are selected randomly from the group's local replies and its allowed
global replies. Reply lists show 10 truncated entries per page and stay in one
editable menu while previewing, deleting, excluding, or changing pages.
Clearing local or global replies requires confirmation.

Telegram flood waits are retried automatically. If Telegram reports that the
bot can no longer access or interact with a group, interactions for that group
are disabled automatically until an administrator enables them again.
If Telegram rejects a configured reaction as invalid, that reaction is removed
from the group's reaction list and auto-reply stays enabled.

Inline manager buttons use Telegram's primary, success, and danger accents to
distinguish navigation, enabling/add actions, and destructive actions.

Configuration changes happen entirely in private chat. Commands, service
events, and messages sent by other bots are ignored.

Every group is recorded as soon as the bot sees any group message or service
event. This makes it immediately eligible for global auto-replies and future
broadcasts unless auto-reply is disabled for that group.

Automatic Telegram text parsing is disabled, so literal text such as
`<message>` and angle brackets inside configured replies remains visible.

After adding the bot as an administrator, send `/autoreply` in the group and
open the private manager. Use **Add Reply**, then send text, a photo, video,
sticker, document, voice note, poll, or another copyable Telegram message.
The bot registers its command menu during startup and responds to `/start` or
`/help` in private chat with setup instructions. `/start` always includes a
Help button and an **Add to Group** button; updates, support, and owner buttons
are shown only when configured. Sudoer commands are hidden from the public
command menu and registered only in each owner/sudoer's private chat. Owner and
sudoers also receive a private **Sudo Panel** button on the start menu.

## Tests

```bash
python3 -m pip install -e ".[dev]"
pytest
```
