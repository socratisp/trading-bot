"""Claude as a news filter, via the Claude Code CLI (`claude -p`) logged in with
your own subscription. Claude can only APPROVE or VETO a setup the code already
built. It never sets prices or size. If Claude is unavailable, the setup goes
through with verdict 'skipped' so the bot keeps working."""
import json
import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

PROMPT = """You are a cautious crypto risk reviewer. A rules-based system proposes this spot trade.
You cannot change the numbers. Decide only whether recent news makes this trade
clearly unwise right now (e.g. exchange hack, regulatory action, major negative
event for this coin, extreme market-wide panic). If nothing clearly negative, approve.

Trade: BUY {pair} at {entry:.4f}, stop {stop:.4f}, target {target:.4f}
Setup: {setup}

Recent headlines:
{news}

Reply with ONLY a JSON object, no other text:
{{"verdict": "approve" or "veto", "reason": "one short sentence"}}"""


def review(setup, news, model="haiku", timeout=120):
    if not shutil.which("claude"):
        return "skipped", "claude CLI not installed"
    prompt = PROMPT.format(news="\n".join(f"- {h}" for h in news) or "- (none)", **setup)
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "skipped", "claude timed out"
    if out.returncode != 0:
        log.warning("claude failed: %s", out.stderr[-300:])
        return "skipped", "claude error (check login / usage limits)"
    return parse(out.stdout)


def parse(stdout):
    try:
        wrapper = json.loads(stdout)
        text = wrapper.get("result", "") if isinstance(wrapper, dict) else str(wrapper)
    except json.JSONDecodeError:
        text = stdout
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return "skipped", "unparseable reply"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "skipped", "unparseable reply"
    verdict = str(data.get("verdict", "")).lower()
    reason = str(data.get("reason", ""))[:200]
    if verdict not in ("approve", "veto"):
        return "skipped", "invalid verdict"
    return verdict, reason
