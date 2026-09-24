"""Each retrain trigger: first model, losing to persistence, and the season closing."""
from datetime import date, timedelta

from roll_call.model import store, train
from tests.test_model_support import con, season, synthetic_history  # noqa: F401  fixtures

IN_SEASON = date(2024, 2, 1)


def _counted_days(con, after: date, n: int) -> list[date]:
    rows = con.execute("""SELECT report_date, count_researchers FROM blue_spring_counts_daily
        WHERE count_researchers IS NOT NULL AND report_date > ? ORDER BY report_date LIMIT ?""",
                       [after, n]).fetchall()
    return rows


def _score_current_model(con, n: int, model_error: float, persistence_error: float) -> date:
    """Store n predictions from the current model that land on counted days. Returns the last
    target date."""
    version = store.current_model(con).model_version
    rows = _counted_days(con, IN_SEASON, n)
    for target, count in rows:
        store.save_prediction(con, made_on=target - timedelta(days=1), target_date=target,
                              predicted=count + model_error, model_version=version,
                              last_count=int(count + persistence_error),
                              last_count_date=target - timedelta(days=1), features={})
    return rows[-1][0]


def test_first_model_needs_enough_rows(con, season):
    synthetic_history(con, winters=1)
    assert train.retrain_if_needed(con, date(2021, 11, 26)) is False
    assert store.current_model(con) is None

    assert train.retrain_if_needed(con, IN_SEASON) is True
    model = store.current_model(con)
    assert model.reason.startswith(train.REASON_FIRST)
    assert model.season_open is True
    # A model exists and it has not been scored: nothing more to do.
    assert train.retrain_if_needed(con, IN_SEASON + timedelta(days=1)) is False


def test_retrain_after_losing_to_persistence(con, season):
    synthetic_history(con)
    train.retrain_if_needed(con, IN_SEASON)
    first = store.current_model(con)
    last = _score_current_model(con, train.PERFORMANCE_WINDOW, model_error=30, persistence_error=5)
    assert train.retrain_if_needed(con, last) is True
    model = store.current_model(con)
    assert model.model_version != first.model_version
    assert model.reason.startswith(train.REASON_LOST)
    assert "30.0 against persistence 5.0" in model.reason
    # The new model has no scored predictions of its own yet.
    assert train.retrain_if_needed(con, last + timedelta(days=1)) is False


def test_no_retrain_while_beating_persistence(con, season):
    synthetic_history(con)
    train.retrain_if_needed(con, IN_SEASON)
    last = _score_current_model(con, train.PERFORMANCE_WINDOW, model_error=5, persistence_error=30)
    assert train.retrain_if_needed(con, last) is False


def test_no_retrain_before_ten_scored_predictions(con, season):
    synthetic_history(con)
    train.retrain_if_needed(con, IN_SEASON)
    last = _score_current_model(con, train.PERFORMANCE_WINDOW - 1, model_error=30,
                                persistence_error=5)
    assert train.retrain_if_needed(con, last) is False


def test_retrain_once_after_the_season_closes(con, season):
    synthetic_history(con)
    train.retrain_if_needed(con, IN_SEASON)
    assert store.current_model(con).season_open is True

    season.open = False
    closed = date(2024, 3, 20)
    assert train.retrain_if_needed(con, closed) is True
    model = store.current_model(con)
    assert model.reason.startswith(train.REASON_SEASON_CLOSED)
    assert model.season_open is False
    assert model.training_rows > store.model_versions(con)[0].training_rows
    assert train.retrain_if_needed(con, closed + timedelta(days=1)) is False
    assert train.retrain_if_needed(con, date(2024, 9, 1)) is False


def test_first_model_trained_off_season_needs_no_season_close_retrain(con, season):
    synthetic_history(con)
    season.open = False
    assert train.retrain_if_needed(con, date(2024, 6, 1)) is True
    assert store.current_model(con).reason.startswith(train.REASON_FIRST)
    assert train.retrain_if_needed(con, date(2024, 6, 2)) is False
