"""Sanitized excerpt — the webhook handlers behind the DTMF-only outreach IVR.

Design rules embodied here:
  - AMD branch FIRST: the menu plays only for a human; machines get a
    purpose-built voicemail message. (The prior conversational-AI vendor
    started speaking on SIP 200 OK — before true pickup — which is the
    disqualifying behavior this design replaced.)
  - <Gather input="dtmf"> exclusively. Speech is never interpreted, so a
    polite "bye bye" can NEVER be mistaken for an opt-out; opting out is an
    explicit, logged keypress (9), honored instantly.
  - lead_id rides in the webhook URLs — identity is structural, keyed by
    construction, never inferred by a model.
  - Audio is pre-rendered human-quality MP3 via <Play> from S3 (per the
    client's "no synthetic voices" directive); a personalized greeting is
    rendered at trigger time and cached by sanitized first name. <Say> TTS
    survives only as a fallback if the greeting render failed.
  - Every outcome lands in three places: the sequence row in DynamoDB, a
    note on the CRM case (the paralegal-facing audit trail), and — via the
    async recording callback — the recording/transcript/PDF chain.
"""
from urllib.parse import urlencode
from xml.sax.saxutils import escape as _xml_escape


def handle_voice(event):
    """POST /voice — initial TwiML. Branch on AMD's AnsweredBy verdict."""
    qs, form = _qs(event), _form(event)
    scenario = qs.get("scenario", DEFAULT_SCENARIO)
    lead_id = qs.get("lead_id", "")
    greeting_url = qs.get("greeting_url", "")
    answered_by = (form.get("AnsweredBy", "") or "").lower()

    # Machine / fax -> voicemail message, no menu, hang up.
    if answered_by.startswith("machine") or answered_by == "fax":
        return twiml_response(_twiml(_play(_static_url("voicemail")), "<Hangup/>"))

    # Human (or 'unknown' — treat as human: better to play the menu than hang up).
    gather_action = f"{API_BASE}/gather?{urlencode({'lead_id': lead_id, 'scenario': scenario})}"
    greeting = _play(greeting_url) if greeting_url else _say(FALLBACK_GREETING)

    gather = (
        f'<Gather input="dtmf" numDigits="1" timeout="20" '
        f'action="{_xml_escape(gather_action)}" method="POST">'
        f"{greeting}"
        f"{_play(_static_url('disclosure'))}"          # recording-consent disclosure
        f"{_play(_static_url(_menu_key(scenario)))}"   # scenario-matched menu
        f"</Gather>"
    )
    fallthrough = _play(_static_url("closing_no_input")) + "<Hangup/>"
    return twiml_response(_twiml(gather, fallthrough))


def handle_gather(event):
    """POST /gather — act on the pressed digit. Digits only, ever."""
    qs, form = _qs(event), _form(event)
    lead_id = qs.get("lead_id", "")
    scenario = qs.get("scenario", DEFAULT_SCENARIO)
    digit = (form.get("Digits", "") or "").strip()

    if digit == "1":                       # connect me with the team
        request_callback(lead_id)
        return twiml_response(_twiml(_play(_static_url("closing_callback")), "<Hangup/>"))
    if digit == "2":                       # send me the link by email
        send_resume_link(lead_id, scenario)
        # Scenario-aware closing: agreement-stage leads hear "sending the
        # AGREEMENT", everyone else hears "sending the LINK".
        return twiml_response(_twiml(_play(_static_url(_email_closing_key(scenario))), "<Hangup/>"))
    if digit == "9":                       # stop calling me — honored instantly
        opt_out(lead_id)
        return twiml_response(_twiml(_play(_static_url("closing_optout")), "<Hangup/>"))

    # Invalid digit: one short re-prompt via a fresh <Gather>, then hang up.
    gather_action = f"{API_BASE}/gather?{urlencode({'lead_id': lead_id, 'scenario': scenario})}"
    reprompt = (
        f'<Gather input="dtmf" numDigits="1" timeout="15" '
        f'action="{_xml_escape(gather_action)}" method="POST">'
        f"{_play(_static_url('reprompt_invalid'))}"
        f"</Gather>"
    )
    return twiml_response(_twiml(reprompt, _play(_static_url("closing_no_input")), "<Hangup/>"))


def handle_status(event):
    """POST /status — record the touch outcome (DynamoDB) + CRM case note."""
    qs, form = _qs(event), _form(event)
    lead_id = qs.get("lead_id", "")
    call_status = (form.get("CallStatus", "") or "").lower()
    answered_by = (form.get("AnsweredBy", "") or "").lower()

    record_touch_outcome(
        lead_id,
        status="answered" if call_status == "completed" else "failed",
        call_sid=form.get("CallSid", ""),
        duration=int(form.get("CallDuration", "0") or 0),
        answered_by=answered_by or "unknown",
    )

    # Human-readable note on the CRM case — the paralegal-facing audit trail.
    if answered_by.startswith("machine"):
        note = "Outreach call reached voicemail. Voicemail left."
    elif call_status == "completed":
        note = f"Outreach call answered ({form.get('CallDuration', '0')}s)."
    else:
        note = f"Outreach call not completed (status: {call_status})."
    add_crm_case_note(lead_id, note, channel="Phone")

    return twiml_response(_twiml())
