# ai_nodes.py
import json
import os
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from prompts import (
    SAFETY_PROMPT,
    QUIZ_SUMMARY_PROMPT,
    PLAN_21D_PROMPT,
    COACH_PROMPT,
    QUIZ_GENERATOR_PROMPT,
    CANONICALIZE_PROMPT
)
from schemas import HabitState, SafetyResult, QuizSummary, Plan21D,QuizForm

load_dotenv()

MODEL_JSON = os.getenv("OPENAI_MODEL_JSON", "gpt-4.1")
MODEL_TEXT = os.getenv("OPENAI_MODEL_TEXT", "gpt-4.1")
def _llm_json(
    prompt: str,
    max_tokens: int = 800,
    temperature: float = 0.5,
    retries: int = 2,
) -> Dict[str, Any]:
    """
    Call the JSON-optimized LLM and return a Python dict.
    Retries with slightly higher temperature and stronger JSON instructions if parsing fails.
    """
    for attempt in range(retries):
        llm = ChatOpenAI(
            model=MODEL_JSON,
            temperature=temperature + (attempt * 0.2),
            response_format={"type": "json_object"},
        )

        resp = llm.invoke(prompt).content

        try:
            return json.loads(resp)
        except Exception:
            # strengthen instructions & increase randomness
            prompt += (
                "\nReturn STRICT JSON. No commentary. "
                "Do NOT repeat previous suggestions."
            )
            continue

    # final fallback if everything fails
    return {}


def _json_llm(temperature: float = 0.3) -> ChatOpenAI:
    """
    Base JSON-optimized LLM (used with structured outputs).
    """
    return ChatOpenAI(
        model=MODEL_JSON,
        temperature=temperature,
    )


def _text_llm(temperature: float = 0.6) -> ChatOpenAI:
    """
    Base text LLM for the coach.
    """
    return ChatOpenAI(
        model=MODEL_TEXT,
        temperature=temperature,
    )



def canonicalize_habit_node(state: HabitState):
    user_raw = state.habit_description or ""

    prompt = CANONICALIZE_PROMPT.format(user_habit_raw=user_raw)
    data = _llm_json(prompt)

    # Fallback if model fails
    canonical = data.get("canonical_habit_name", user_raw)
    category = data.get("habit_category", "unknown")
    conf = data.get("confidence", "low")

    return {
        "canonical_habit_name": canonical,
        "habit_category": category,
        "canonical_confidence": conf,
    }


# ---------- Safety Node ----------

def safety_node(state: HabitState) -> Dict[str, Any]:
    """
    Classify the latest user text for safety and scope.

    Uses SafetyResult:
    - risk: "none" | "self_harm" | "eating_disorder" | "severe_addiction" | "violence" | "other"
    - action: "allow" | "block_and_escalate"
    - message: short, safe helper text
    """

    # Prefer the freshest user message; fall back to habit_description or empty string
    user_text = (
        getattr(state, "last_user_message", None)
        or getattr(state, "habit_description", None)
        or getattr(state, "user_input", "")
        or ""
    )

    prompt = SAFETY_PROMPT.format(user_text=user_text)

    llm = _json_llm(temperature=0.1)
    structured_llm = llm.with_structured_output(SafetyResult)

    try:
        safety = structured_llm.invoke(prompt)
    except Exception:
        # Be conservative if safety fails: block & escalate instead of silently allowing
        safety = SafetyResult(
            risk="other",
            action="block_and_escalate",
            message=(
                "I’m here only for habit and behavior coaching, so I can’t safely respond to this. "
                "Please avoid medical, illegal, or harmful topics, and consider reaching out to a "
                "trusted person or local professional if you’re in distress."
            ),
        )

    return {"safety": safety}



def quiz_form_node(state: HabitState) -> Dict[str, Any]:
    """
    Generate a tailored 8–10 question quiz based on the user's habit description.

    Guarantees:
    - The habit name / product (e.g. "Zyn", "TikTok", "porn") is preserved.
    - Questions are explicitly about THIS habit, not generic behavior.
    """
    habit_description = state.habit_description or ""

    prompt = QUIZ_GENERATOR_PROMPT.format(
        habit_description=habit_description
    )

    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL_JSON", "gpt-4.1-mini"),
        temperature=0.4,
    )
    structured_llm = llm.with_structured_output(QuizForm)

    try:
        quiz_form = structured_llm.invoke(prompt)
    except Exception:
        # Fallback that is STILL tied to the described habit
        habit_label = habit_description or "this habit"
        quiz_form = QuizForm(
            habit_name_guess=habit_label,
            questions=[
                {
                    "id": "q1",
                    "question": f"In your own words, what does {habit_label} look like for you?",
                    "helper_text": "Describe what you do, what you use, and how it usually happens.",
                },
                {
                    "id": "q2",
                    "question": f"How often do you usually do {habit_label} in a day or week?",
                    "helper_text": None,
                },
                {
                    "id": "q3",
                    "question": f"At what times of day does {habit_label} usually happen?",
                    "helper_text": "For example: late night, after work, during breaks, etc.",
                },
                {
                    "id": "q4",
                    "question": f"Where are you most often when {habit_label} happens?",
                    "helper_text": "Bedroom, bathroom, desk, outside, with friends, etc.",
                },
                {
                    "id": "q5",
                    "question": f"What are you usually feeling right before {habit_label}?",
                    "helper_text": "Bored, stressed, lonely, tired, anxious, excited, etc.",
                },
                {
                    "id": "q6",
                    "question": f"What tends to trigger {habit_label} most often?",
                    "helper_text": "People, places, apps, notifications, objects, situations, etc.",
                },
                {
                    "id": "q7",
                    "question": f"Have you tried changing {habit_label} before? What worked or failed?",
                    "helper_text": None,
                },
                {
                    "id": "q8",
                    "question": f"Why do you want to reduce or change {habit_label} now?",
                    "helper_text": "What matters most to you here?",
                },
                {
                    "id": "q9",
                    "question": f"In which situations is {habit_label} hardest to control?",
                    "helper_text": "Specific times, people, places, or moods.",
                },
            ],
        )

    return {"quiz_form": quiz_form}

# ---------- Quiz Summary Node ----------

def quiz_summary_node(state: HabitState) -> Dict[str, Any]:
    """
    Convert:
    - original habit_description
    - AI-generated quiz_form
    - user_quiz_answers

    into a compact QuizSummary JSON.
    """
    habit_description = state.habit_description or ""
    quiz_form_json = state.quiz_form.model_dump() if state.quiz_form else {}
    user_quiz_answers = state.user_quiz_answers or ""

    # THIS is where the error was: we MUST pass quiz_form_json
    prompt = QUIZ_SUMMARY_PROMPT.format(
        habit_description=habit_description,
        quiz_form_json=json.dumps(quiz_form_json, ensure_ascii=False),
        user_quiz_answers=user_quiz_answers,
    )

    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL_JSON", "gpt-4.1-mini"),
        temperature=0.3,
    )
    structured_llm = llm.with_structured_output(QuizSummary)

    try:
        summary = structured_llm.invoke(prompt)
    except (ValidationError, Exception):
        # Defensive fallback – still honest, no hallucinated structure
        summary = QuizSummary(
            user_habit_raw=habit_description,
            canonical_habit_name=habit_description or "user habit",
            habit_category="other",
            category_confidence="low",
            product_type="unspecified",
            severity_level="mild",
            main_trigger="unknown",
            peak_times="unknown",
            common_locations="unknown",
            emotional_patterns="unclear",
            frequency_pattern="unknown",
            previous_attempts="not_clear",
            motivation_reason="user_wants_change",
            risk_situations="unknown",
        )

    return {"quiz_summary": summary}


def _category_guidance(summary: QuizSummary) -> str:
    """
    Rich category- and user-specific guidance so each habit type
    produces a structurally different 21-day plan.

    This is injected into the PLAN_21D_PROMPT as extra context.
    """

    cat = (summary.habit_category or "other").lower()
    severity = summary.severity_level
    name = summary.canonical_habit_name or summary.user_habit_raw or "the habit"
    raw = summary.user_habit_raw or ""
    trigger = summary.main_trigger or "unclear triggers"
    peak = summary.peak_times or "unclear peak times"
    loc = summary.common_locations or "unclear locations"
    emo = summary.emotional_patterns or "unclear emotional patterns"
    freq = summary.frequency_pattern or "unclear frequency"
    motive = summary.motivation_reason or "unclear motivation"
    risk = summary.risk_situations or "unclear risk situations"
    prev = summary.previous_attempts or "not clearly described"

    base_context = f"""
User-specific context:
- Exact wording: {raw}
- Canonical habit name: {name}
- Severity: {severity}
- Main trigger: {trigger}
- Peak times: {peak}
- Common locations: {loc}
- Emotional pattern: {emo}
- Frequency pattern: {freq}
- Motivation: {motive}
- High-risk situations: {risk}
- Previous attempts: {prev}

Plan must explicitly reference these details across the 21 days.
"""

    # Now add deep category-specific strategy
    if cat in ["nicotine_smoking", "nicotine_vaping", "nicotine_oral"]:
        cat_block = f"""
Category: Nicotine

Core strategy:
- Treat {name} as a dopamine and ritual loop, not just a chemical.
- Emphasize routines around peak times (for example {peak}), and environments like {loc}.
- Explicitly build friction around storage, access, purchase, and first use of the day.
- For oral products like pouches, include mouth and hand substitution tasks.
- For higher severity, include more aggressive environment restructuring and longer urge delays.

Must include across 21 days:
- At least 4 tasks about changing where {name} is kept or accessed.
- At least 4 tasks about first use of the day and last use window.
- At least 3 tasks about physical state regulation during withdrawal (sleep window, hydration, body movement).
- At least 3 tasks that use emotional patterns like {emo} to pre-empt urges.
"""

    elif cat == "pornography":
        cat_block = f"""
Category: Pornography / sexual content

Core strategy:
- Treat {name} as a privacy plus device plus emotional loop.
- Focus on device rules, room layout, and late-night behaviour, especially around {peak}.
- Explicitly design friction around entering high-risk locations such as {loc}.
- Use stimulus control (lights, door, blockers, charging locations) instead of just "willpower".
- Tie reflection tasks to shame cycles and emotion patterns like {emo}, but without using shame language.

Must include across 21 days:
- At least 4 tasks that change how and where the device is used.
- At least 3 tasks that pre-empt late-night or alone-time triggers.
- At least 3 tasks that redirect immediately after a strong urge into a specific alternative behaviour.
- At least 2 tasks that review a slip in a non-judgmental, purely diagnostic way.
"""

    elif cat in ["screen_time", "social_media", "gaming"]:
        cat_block = f"""
Category: Screen-based habit (social media, scrolling, or gaming)

Core strategy:
- Treat {name} as an algorithm plus environment plus boredom loop.
- Focus on first and last 30 minutes of the day, especially if peak times include {peak}.
- Redesign notification logic, home screen layout, and app availability.
- Use strong "screen zones" and "screen windows" instead of unrealistic total bans.
- Tie replacement activities to the motivation: {motive}.

Must include across 21 days:
- At least 3 tasks modifying notifications, app positions, or app removal.
- At least 3 tasks that change morning behaviour before the first use.
- At least 3 tasks that change evening behaviour and pre-sleep routines.
- At least 3 tasks that deliberately swap a high-risk scrolling window with something aligned to {motive}.
"""

    elif cat in ["alcohol", "cannabis"]:
        cat_block = f"""
Category: Substance use (alcohol or cannabis)

Core strategy:
- Treat {name} as a context plus people plus emotional regulation loop.
- Focus on social settings, routes, and specific times like {peak}.
- Include clear "no-use" contexts and re-routing strategies for high-risk places like {loc}.
- Include craving delay plus alternative rituals at the exact times they usually use.
- Tie medium-term tasks to motivation {motive} and long-term identity.

Must include across 21 days:
- At least 3 tasks that alter routes or places that usually lead to use.
- At least 3 tasks that create explicit "no-use" rules in specific contexts.
- At least 3 tasks focused on high-risk situations described as {risk}.
- At least 2 tasks rehearsing what to do during a social invite or stress spike.
"""

    elif cat in ["sugar", "food_overeating"]:
        cat_block = f"""
Category: Food / sugar / overeating

Core strategy:
- Treat {name} as a kitchen plus shopping plus emotional soothing loop.
- Focus on visibility and proximity of foods, especially around locations like {loc}.
- Tie tasks to emotional states like {emo} and times like {peak}.
- Include shopping list and preparation changes that reduce impulsive access.
- Use small plate, portion, and environment tricks rather than "never eat X again" rules.

Must include across 21 days:
- At least 3 tasks about shopping or preparing alternatives in advance.
- At least 3 tasks about changing visibility and proximity of trigger foods.
- At least 3 tasks about emotional check-ins before eating in high-risk moments.
- At least 2 tasks about how to handle evenings or specific risk situations like {risk}.
"""

    elif cat in ["shopping_spending", "gambling"]:
        cat_block = f"""
Category: Spending / gambling

Core strategy:
- Treat {name} as a excitement plus access plus impulse loop.
- Focus on financial access: cards, apps, cash, sites, groups.
- Use strong pre-commitment rules, delays, and visibility of consequences.
- Tie specific tasks to high-risk times or contexts like {peak} and {risk}.
- Use replacement forms of excitement or reward that are lower-risk.

Must include across 21 days:
- At least 3 tasks about restricting or delaying financial access.
- At least 3 tasks about changing what happens in the 10–20 minutes before spending or betting.
- At least 2 tasks about reviewing a past spending or gambling episode analytically, not emotionally.
- At least 2 tasks that explicitly reinforce the motivation: {motive}.
"""

    elif cat == "procrastination":
        cat_block = f"""
Category: Procrastination

Core strategy:
- Treat {name} as avoidance of a specific type of work or feeling.
- Tie tasks directly to the kind of work they avoid most (for example study or deep work).
- Use very small, clear start behaviours instead of vague discipline tasks.
- Design environment and time box rules around the true peak avoidance windows like {peak}.
- Link identity work to becoming someone who handles {trigger} with short, focused bursts.

Must include across 21 days:
- At least 5 tasks that define a tiny, concrete starting action (for example open document and write one sentence).
- At least 3 tasks that reduce distractions in the main work location {loc}.
- At least 3 tasks that handle emotional patterns like {emo} before work instead of during.
- At least 2 tasks that rehearse what to do after a bad day without abandoning the plan.
"""

    else:
        cat_block = f"""
Category: Other or unclear

Core strategy:
- The category label is not precise, so lean heavily on the user's actual patterns.
- Design tasks explicitly around the main trigger {trigger}, peak times {peak}, and locations {loc}.
- Use emotional pattern {emo} to time interventions before the urge becomes very strong.
- Apply standard habit-breaking tools: friction, replacement, identity, slip recovery, environment design.

Must include across 21 days:
- At least 5 tasks that directly reference the described triggers, times, or locations.
- At least 3 tasks that practice urge delay plus a named replacement behaviour.
- At least 2 tasks that explicitly connect daily actions to the motivation: {motive}.
"""

    return base_context + "\n" + cat_block


# ---------- 21-Day Plan Node ----------

def _fallback_plan21(quiz_summary: Optional[QuizSummary] = None) -> Plan21D:
    """
    Fallback 21-day plan if the LLM output fails validation.

    Uses QuizSummary if available (canonical_habit_name, main_trigger, motivation_reason),
    but does NOT rely on any old fields like 'habit_name'.
    """
    if quiz_summary:
        habit = (
            quiz_summary.canonical_habit_name
            or quiz_summary.user_habit_raw
            or "your habit"
        )
        trigger = quiz_summary.main_trigger or "your usual triggers"
        motive = quiz_summary.motivation_reason or "your reasons for change"
    else:
        habit = "your habit"
        trigger = "your usual triggers"
        motive = "your reasons for change"

    plan_summary = (
        f"This 21-day plan helps you reduce {habit} with small daily actions, "
        f"focusing on awareness, friction around {trigger}, and identity shifts based on {motive}."
    )

    day_tasks = {
        "day_1":  f"Write down when and why {habit} usually happens. No pressure to change yet.",
        "day_2":  f"Before each urge for {habit}, pause 30 seconds and name what you’re feeling.",
        "day_3":  f"Move one step further from your usual {trigger} location before acting.",
        "day_4":  f"Choose a 5-minute healthy activity to try once when an urge appears.",
        "day_5":  f"Disable one small cue that feeds {habit} (notification, tab, app, or object).",
        "day_6":  f"Set a clear daily cutoff time after which you do not allow {habit}.",
        "day_7":  "Slip-recovery: review this week, note one pattern, and adjust cutoff time if needed.",
        "day_8":  f"Delay {habit} by 5 minutes once today and do your chosen healthy activity first.",
        "day_9":  f"Change your usual {habit} location; do it somewhere less comfortable if you must.",
        "day_10": f"Tell future-you in a note why reducing {habit} matters over the next 3 months.",
        "day_11": f"Reduce one typical {habit} episode by half in time, intensity, or frequency.",
        "day_12": "Plan a simple evening routine that does not include your main trigger source.",
        "day_13": f"Practice one ‘urge surfing’ cycle: breathe, observe, and let one urge pass unacted.",
        "day_14": "Slip-recovery: list three things that went well and one small adjustment for next week.",
        "day_15": f"Define a rule: one specific situation where {habit} is no longer allowed at all.",
        "day_16": f"Replace one full {habit} episode with your healthy alternative, start to finish.",
        "day_17": "Prepare your environment tonight so tomorrow’s first hour is completely trigger-free.",
        "day_18": f"Teach someone (or journal) one insight you’ve learned about your {habit} triggers.",
        "day_19": "Create a 2-sentence identity statement about who you’re becoming without this habit.",
        "day_20": "Plan how you will keep these limits and routines going after Day 21.",
        "day_21": "Review progress, refresh your identity statement, and choose one long-term keystone rule.",
    }

    return Plan21D(plan_summary=plan_summary, day_tasks=day_tasks)

def plan21_node(state: HabitState) -> Dict[str, Any]:
    """
    Generate the 21-day plan using the QuizSummary as context
    + category-specific guidance so different habits feel truly different.
    """
    if not state.quiz_summary:
        return {"plan21": _fallback_plan21(None)}

    quiz_json = state.quiz_summary.model_dump()
    guidance = _category_guidance(state.quiz_summary)

    prompt = PLAN_21D_PROMPT.format(
        quiz_summary_json=json.dumps(quiz_json, ensure_ascii=False),
        category_guidance=guidance,
    )

    # 🔹 Use your JSON LLM helper, NOT MODEL_JSON, NOT _json_llm
    data = _llm_json(prompt, max_tokens=1600, temperature=0.35)

    try:
        # Basic sanitization
        day_tasks = data.get("day_tasks", {}) or {}
        for i in range(1, 21):
            key = f"day_{i}"
            if key not in day_tasks or not isinstance(day_tasks[key], str) or not day_tasks[key].strip():
                day_tasks[key] = _fallback_plan21(state.quiz_summary).day_tasks[key]

        data["day_tasks"] = day_tasks

        if "plan_summary" not in data or not isinstance(data["plan_summary"], str):
            data["plan_summary"] = (
                f"Personalized 21-day behavioural plan to reduce {state.quiz_summary.canonical_habit_name}."
            )

        plan = Plan21D(**data)
    except:
        plan = _fallback_plan21(state.quiz_summary)

    return {"plan21": plan}



# ---------- Coach Node ----------

def coach_node(state: HabitState) -> Dict[str, Any]:
    """
    Context-aware AI coach that uses:
    - safety (to block out-of-scope / dangerous requests)
    - quiz_summary
    - plan21
    - chat_history
    - last_user_message
    """

    # 1) Hard safety block for medical / illegal / minors / self-harm / violence / etc.
    # With the new SafetyResult, we check `action`, not `status`.
    safety = state.safety
    if safety is not None and getattr(safety, "action", None) == "block_and_escalate":
        reply = (
            "I’m here only for habit and behavior coaching, so I can’t help with medical, legal, "
            "explicit, or illegal requests. If this is about your health, safety, or a serious "
            "situation, please talk to a qualified professional or someone you trust in real life."
        )

        # update chat history even on blocked replies
        new_history = list(state.chat_history or [])
        user_message = state.last_user_message or state.habit_description or ""
        if user_message:
            new_history.append({"role": "user", "content": user_message})
        new_history.append({"role": "assistant", "content": reply})

        return {
            "coach_reply": reply,
            "chat_history": new_history,
        }

    # 2) Normal coaching flow (safe content)
    quiz_json = state.quiz_summary.model_dump() if state.quiz_summary else {}
    plan_json = state.plan21.model_dump() if state.plan21 else {}

    # Format history
    history_lines = []
    for msg in state.chat_history or []:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        history_lines.append(f"{role}: {content}")
    history_text = "\n".join(history_lines)

    user_message = state.last_user_message or state.habit_description or ""

    base_prompt = COACH_PROMPT + "\n\n"
    base_prompt += f"quiz_summary_json:\n{json.dumps(quiz_json, ensure_ascii=False)}\n\n"
    base_prompt += f"plan_21d_json:\n{json.dumps(plan_json, ensure_ascii=False)}\n\n"
    base_prompt += f"history_text:\n{history_text}\n\n"
    base_prompt += f"user_message:\n{user_message}\n"

    llm = _text_llm()
    try:
        reply = llm.invoke(base_prompt).content.strip()
    except Exception:
        reply = "Let’s focus on one small step you can do today that matches your plan."

    # update chat history
    new_history = list(state.chat_history or [])
    if user_message:
        new_history.append({"role": "user", "content": user_message})
    new_history.append({"role": "assistant", "content": reply})

    return {
        "coach_reply": reply,
        "chat_history": new_history,
    }

