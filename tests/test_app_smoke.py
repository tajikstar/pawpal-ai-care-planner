"""Headless check of the Streamlit app: draft a plan offline, approve it, see it in the schedule."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_ai_planner_flow_in_streamlit(monkeypatch):
    # Force the offline planner: hide any keys, and stop app.py from re-reading them from .env.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception

    at.text_area[0].input("I just adopted a 4-month-old puppy named Biscuit and I work 9 to 5.")
    at.button[0].click().run()
    assert not at.exception
    assert len(at.checkbox) >= 4  # one checkbox per proposed task

    add = next(b for b in at.button if "approved task" in b.label)
    add.click().run()
    assert not at.exception
    owner = at.session_state.owner
    assert [p.name for p in owner.pets] == ["Biscuit"]
    assert len(owner.get_all_tasks()) >= 4
