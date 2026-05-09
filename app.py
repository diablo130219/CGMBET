import csv
import io
import os
import sqlite3
from datetime import date, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")

DATABASE = os.environ.get("DATABASE_PATH", "cgmbet.db")
STRATEGIES = ["GG", "Over 2.5", "Over 1.5"]


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            match_date TEXT,
            match_time TEXT,
            championship TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            market TEXT,
            odd REAL DEFAULT 0,
            prob_home TEXT DEFAULT '',
            prob_away TEXT DEFAULT '',
            elo_gap TEXT DEFAULT '',
            gg_home TEXT DEFAULT '',
            gg_away TEXT DEFAULT '',
            over_home TEXT DEFAULT '',
            over_away TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    ensure_column(conn, "matches", "prob_home", "TEXT DEFAULT ''")
    ensure_column(conn, "matches", "prob_away", "TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def parse_float(value):
    if value is None:
        return 0
    value = str(value).replace(",", ".").strip().replace("%", "")
    try:
        return float(value)
    except ValueError:
        return 0


def pick(row, names):
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for wanted in names:
        wanted = wanted.lower()
        for key, value in lowered.items():
            if wanted in key:
                return str(value or "").strip().replace('"', "")
    return ""


def detect_delimiter(text):
    first = text.splitlines()[0] if text.splitlines() else ""
    return ";" if first.count(";") >= first.count(",") else ","


def probability_for_strategy(row, strategy):
    if strategy == "GG":
        return pick(row, ["quota gg", "prob gg", "score gg", "gg %", "gg", "quota"])
    if strategy == "Over 2.5":
        return pick(row, ["quota over 2.5", "prob over 2.5", "score over 2.5", "over 2.5", "quota"])
    if strategy == "Over 1.5":
        return pick(row, ["quota over 1.5", "prob over 1.5", "score over 1.5", "over 1.5", "quota"])
    return pick(row, ["quota", "prob", "score"])


def home_probability(row):
    return pick(row, [
        "probabilità casa", "probabilita casa", "prob casa", "casa %",
        "home probability", "home prob", "prob home", "1 %", "score casa"
    ])


def away_probability(row):
    return pick(row, [
        "probabilità trasferta", "probabilita trasferta", "prob trasferta", "trasferta %",
        "away probability", "away prob", "prob away", "2 %", "score trasferta"
    ])


def get_counts(conn):
    total_all = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    gg_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'GG'").fetchone()[0]
    over25_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'Over 2.5'").fetchone()[0]
    over15_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'Over 1.5'").fetchone()[0]
    return total_all, gg_count, over25_count, over15_count


def get_sideboard_matches(conn):
    sideboard = {}
    for s in STRATEGIES:
        sideboard[s] = conn.execute(
            "SELECT * FROM matches WHERE strategy = ? ORDER BY match_date ASC, match_time ASC LIMIT 3",
            (s,)
        ).fetchall()
    return sideboard


@app.route("/login")
def login():
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    conn = get_db()
    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    strategy_counts = {"GG": gg_count, "Over 2.5": over25_count, "Over 1.5": over15_count}

    today = date.today()
    today_count = conn.execute("SELECT COUNT(*) FROM matches WHERE match_date = ?", (today.isoformat(),)).fetchone()[0]
    next7_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=7)).isoformat())
    ).fetchone()[0]

    avg_odds = {}
    for s in STRATEGIES:
        avg = conn.execute("SELECT AVG(odd) FROM matches WHERE strategy = ? AND odd > 0", (s,)).fetchone()[0]
        avg_odds[s] = round(avg, 2) if avg else 0

    sideboard_matches = get_sideboard_matches(conn)
    conn.close()

    return render_template(
        "dashboard.html",
        strategy_counts=strategy_counts,
        today_count=today_count,
        next7_count=next7_count,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
        avg_odds=avg_odds,
        sideboard_matches=sideboard_matches,
    )


@app.route("/")
def index():
    strategy = request.args.get("strategy", "GG")
    search = request.args.get("search", "").strip()
    date_filter = request.args.get("date_filter", "")

    query = "SELECT * FROM matches WHERE strategy = ?"
    params = [strategy]

    if search:
        query += " AND (home_team LIKE ? OR away_team LIKE ? OR championship LIKE ? OR notes LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like, like])

    if date_filter == "today":
        query += " AND match_date = ?"
        params.append(date.today().isoformat())

    if date_filter == "3days":
        query += " AND match_date BETWEEN ? AND ?"
        params.append(date.today().isoformat())
        params.append((date.today() + timedelta(days=3)).isoformat())

    if date_filter == "7days":
        query += " AND match_date BETWEEN ? AND ?"
        params.append(date.today().isoformat())
        params.append((date.today() + timedelta(days=7)).isoformat())

    query += " ORDER BY match_date ASC, match_time ASC, championship ASC"

    conn = get_db()
    matches = conn.execute(query, params).fetchall()
    total_strategy = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = ?", (strategy,)).fetchone()[0]
    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    sideboard_matches = get_sideboard_matches(conn)
    conn.close()

    return render_template(
        "index.html",
        matches=matches,
        strategy=strategy,
        search=search,
        date_filter=date_filter,
        total=len(matches),
        total_strategy=total_strategy,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
        sideboard_matches=sideboard_matches,
    )


@app.route("/import", methods=["POST"])
def import_csv():
    strategy = request.form.get("strategy", "GG")
    file = request.files.get("csv_file")

    if not file:
        flash("Nessun file caricato.", "error")
        return redirect(url_for("index", strategy=strategy))

    text = file.read().decode("utf-8-sig", errors="ignore")
    delimiter = detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    imported = 0
    conn = get_db()

    for row in reader:
        home = pick(row, ["squadra casa", "casa", "home"])
        away = pick(row, ["squadra ospite", "trasferta", "away", "ospite"])

        if not home or not away:
            continue

        conn.execute(
            """
            INSERT INTO matches (
                strategy, match_date, match_time, championship, home_team, away_team,
                market, odd, prob_home, prob_away, elo_gap, gg_home, gg_away, over_home, over_away, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy,
                pick(row, ["data", "date"]),
                pick(row, ["ora", "time"]),
                pick(row, ["campionato", "league", "lega"]),
                home,
                away,
                strategy,
                parse_float(probability_for_strategy(row, strategy)),
                home_probability(row),
                away_probability(row),
                pick(row, ["elo gap", "elo"]),
                pick(row, ["gg casa"]),
                pick(row, ["gg trasferta"]),
                pick(row, ["over casa"]),
                pick(row, ["over trasferta"]),
                "",
            ),
        )
        imported += 1

    conn.commit()
    conn.close()

    flash(f"{imported} partite importate nella strategia {strategy}.", "success")
    return redirect(url_for("index", strategy=strategy))


@app.route("/clear/<strategy>", methods=["POST"])
def clear_strategy(strategy):
    conn = get_db()
    conn.execute("DELETE FROM matches WHERE strategy = ?", (strategy,))
    conn.commit()
    conn.close()
    flash(f"Dati della strategia {strategy} cancellati.", "success")
    return redirect(url_for("index", strategy=strategy))


@app.route("/export/<strategy>")
def export_strategy(strategy):
    conn = get_db()
    rows = conn.execute("SELECT * FROM matches WHERE strategy = ? ORDER BY match_date, match_time", (strategy,)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Strategia", "Data", "Ora", "Campionato", "Casa", "Trasferta",
        "Mercato", "Probabilità", "Prob Casa", "Prob Trasferta", "ELO GAP"
    ])

    for m in rows:
        writer.writerow([
            m["strategy"], m["match_date"], m["match_time"], m["championship"],
            m["home_team"], m["away_team"], m["market"], m["odd"],
            m["prob_home"], m["prob_away"], m["elo_gap"]
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)

    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=f"dati_{strategy.replace(' ', '_')}.csv")


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
