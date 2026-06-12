# Telegram Group Interaction Bot

A Kurigram bot that interacts with eligible human messages in a group. It
rotates through group-specific replies and sometimes adds a random reaction.

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

## Group commands

Group administrators can use:

- `/autoreply on` and `/autoreply off`
- `/autoreply_add <text>`
- `/autoreply_remove <number>`
- `/autoreply_list`
- `/autoreply_clear`
- `/autoreply_status`
- `/autoreply_help`
- `/reaction_on` and `/reaction_off`
- `/reaction_chance <0-100>`
- `/reaction_add <emoji>`
- `/reaction_remove <emoji>`
- `/reaction_list`

Commands are admin-only. Commands, service events, and messages sent by other
bots are ignored. Enabling auto-replies also acts as the master switch for all
interactions. Random reactions default to a 25% chance.

After adding the bot as an administrator, run `/autoreply on`. Text replies
also require at least one message added with `/autoreply_add <text>`.
The bot registers its command menu during startup and responds to `/start` or
`/help` in private chat with setup instructions.

## Tests

```bash
python3 -m pip install -e ".[dev]"
pytest
```
