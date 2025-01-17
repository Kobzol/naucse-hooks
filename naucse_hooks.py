import logging.handlers
import re
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Dict, Optional

import requests
import yaml
import giturlparse
from arca import Arca, CurrentEnvironmentBackend
from flask import Flask, request, jsonify, session, flash, redirect, url_for, render_template
from flask_github import GitHub, GitHubError
from flask_session import Session
from travispy import TravisPy
from raven.contrib.flask import Sentry
from travispy.travispy import PRIVATE

app = Flask(__name__)
app.config.from_pyfile("settings.cfg")
app.config.from_pyfile("local_settings.cfg", silent=True)
sentry = Sentry(app, dsn=app.config["SENTRY_DSN"])

handler = logging.handlers.RotatingFileHandler("naucse_hooks.log")
formatter = logging.Formatter("[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s")

handler.setLevel(logging.INFO)
handler.setFormatter(formatter)

app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

arca = Arca(backend=CurrentEnvironmentBackend(
    current_environment_requirements=None,
))
github = GitHub(app)
Session(app)


# {repo: {branch: commit}}
last_commit: Dict[str, Dict[str, str]] = defaultdict(lambda: defaultdict(dict))


def get_latest_naucse() -> Path:
    """ Triggers an pull, returns the path to the pulled repository.
    """
    _, path = arca.get_files(app.config["NAUCSE_GIT_URL"], app.config["NAUCSE_BRANCH"])

    return path


def get_last_commit_in_branch(repo, branch):
    parsed = giturlparse.parse(repo)

    if not parsed.valid:
        return None

    if not parsed.github:
        return None

    url = f"https://api.github.com/repos/{parsed.owner}/{parsed.repo}/commits/{branch}"

    try:
        response = requests.get(url)
        assert response.status_code == 200
        return response.json()["sha"]
    except BaseException:
        sentry.captureException()
        return None


def _iterate(folder: Path):
    """ Recursive function which iterates over a folder contents,
        going deeper to folders and yielding link parsed link files
    """
    for child in folder.glob("**/link.yml"):  # type: Path
        fork = yaml.safe_load(child.read_text())
        yield fork


def iterate_forks() -> Iterator[Dict[str, str]]:
    """ Pulls naucse and iterates over all files in the repository, yielding all links
    """
    naucse_path = get_latest_naucse()

    yield from _iterate(naucse_path)


def normalize_repo(repo) -> str:
    """ Normalizes git repo url so it's easier to compare two urls
    """
    repo = re.sub(r"^http[s]?://", "", repo)
    repo = re.sub(r".git$", "", repo)
    repo = re.sub(r"/$", "", repo)

    return repo


def same_repo(repo1, repo2) -> bool:
    """ Compares two repo urls if they're the same, disregarding protocol (http/https) and .git at the end.
    """
    return normalize_repo(repo1) == normalize_repo(repo2)


def is_branch_in_naucse(repo: str, branch: str) -> bool:
    """ Checks if a pushed branch is used in naucse somewhere
    """
    for fork in iterate_forks():
        if fork.get("branch", "master").strip() == branch.strip() and same_repo(fork["repo"], repo):
            return True
    return False


def trigger_build(repo, branch):
    """ Sends a request to Travis, rebuilding the content
    """
    if not app.config["TRAVIS_REPO_SLUG"] or not app.config["TRAVIS_TOKEN"]:
        return

    t = TravisPy(app.config['TRAVIS_TOKEN'], uri=PRIVATE)

    # it doesn't make sense for multiple builds of the same branch to run at the same time
    # so if some are still running for our target branch, lets stop them
    for build in t.builds(slug=app.config["TRAVIS_REPO_SLUG"]):
        if not build.pull_request and build.pending and build.commit.branch == app.config["NAUCSE_BRANCH"]:
            build.cancel()

    # unfortunately, TravisPy doesn't provide a method to trigger a build, so triggering manually:
    response = requests.post(
        "https://api.travis-ci.com/repo/{}/requests".format(
            urllib.parse.quote_plus(app.config["TRAVIS_REPO_SLUG"])
        ),
        json={
            "request": {
                "branch": app.config["NAUCSE_BRANCH"],
                "message": f"Triggered by {repo}/{branch}"
            }
        },
        headers={
            "Authorization": f"token {app.config['TRAVIS_TOKEN']}",
            "Travis-API-Version": "3"
        }
    )
    response.raise_for_status()


def get_branch_from_ref(ref: str) -> Optional[str]:
    if ref.startswith("refs/heads/"):
        return ref.replace("refs/heads/", "")

    if ref.startswith("refs/tags/"):
        return ref.replace("refs/tags/", "")

    return None


@app.route('/')
@app.route('/all/', defaults={"all": True}, endpoint="all")
def index(all=False):
    access_token = session.get("github_access_token")
    repos = None

    if access_token is not None:
        user = github.get("user")

        repos = github.get("user/repos", all_pages=True, data={"visibility": "public"})

        if all:
            # list naucse.python.cz repos first
            # then all the users repos ordered by name
            # then the rest ordered by the name of the org and then by the name
            # (False is sorted before True)
            repos.sort(key=lambda x: (x["name"] != "naucse.python.cz",
                                      x["owner"]["login"] != user["login"],
                                      x["full_name"]))
        else:
            repos = [x for x in repos if x["name"] == "naucse.python.cz"]
            repos.sort(key=lambda x: (x["owner"]["login"] != user["login"], x["full_name"]))

    return render_template("index.html",
                           logged_in=access_token is not None,
                           repos=repos,
                           all=all)


@app.route('/login')
def login():
    return github.authorize("public_repo,write:repo_hook")


@app.route('/logout')
def logout():
    session.pop("github_access_token")

    return redirect(url_for('index'))


@app.route('/activate/<string:login>/<string:name>/')
def activate(login, name):
    access_token = session.get("github_access_token")
    if access_token is None:
        flash("Nejste přihlášený/á!", "error")
        return redirect(url_for("index"))

    try:
        github.get(f"repos/{login}/{name}")
    except GitHubError:
        flash(f"Repozitář {login}/{name} neexistuje nebo k němu nemáte přístup!", "error")
        return redirect(url_for(("index")))

    try:
        hooks = github.get(f"repos/{login}/{name}/hooks")
    except GitHubError:
        flash(f"U repozitáře {login}/{name} nemůžete číst webhooky!", "error")
        return redirect(url_for(("index")))

    already_exists = False

    for hook in hooks:
        if hook.get("config", {}).get("url") == app.config["PUSH_HOOK"]:
            already_exists = True
            break

    if already_exists:
        flash(f"Webhook již v repozitáři {login}/{name} existuje!", "warning")
        return redirect(url_for("index"))

    try:
        github.post(f"repos/{login}/{name}/hooks", {
            "name": "web",
            "config": {
                "url": app.config["PUSH_HOOK"],
                "content_type": "json"
            }
        })
        flash(f"Webhook nainstalován do repozitáře {login}/{name}!", "success")
    except GitHubError:
        flash(f"Webhook se do repozitáře {login}/{name} nepovedlo nainstalovat, nejspíše nemáte právo ho přidávat.",
              "error")

    return redirect(url_for("index"))


@app.route('/github-callback')
@github.authorized_handler
def authorized(oauth_token):
    if oauth_token is None:
        flash("Přihlášení selhalo!", "error")
        return redirect(url_for('index'))

    session["github_access_token"] = oauth_token

    return redirect(url_for('index'))


@github.access_token_getter
def token_getter():
    return session.get("github_access_token")


@app.route('/hooks/push', methods=["POST"])
def push_hook():
    def invalid_request(text=None):
        return jsonify({
            "error": text or "Invalid request"
        }), 400

    github_event = request.headers.get("X-GitHub-Event")

    body = request.get_json(silent=True, force=True)
    repo = (body or {}).get("repository", {}).get("clone_url")

    if github_event is None:
        app.logger.warning("X-GitHub-Event header missing from repo %s", repo)
        app.logger.debug("Body: %s", body)
        return invalid_request("X-GitHub-Event header missing")
    elif github_event == "ping":
        app.logger.info("Responded to a ping event from repo %s", repo)
        return jsonify({
            "success": "Hook works!"
        })
    elif github_event != "push":
        app.logger.warning("Invalid X-GitHub-Event %s from repo %s", github_event, repo)
        return invalid_request("Invalid X-GitHub-Event header, only accepting ping and push.")

    if body is None:
        app.logger.warning("Could not decode body to JSON.")
        app.logger.debug(request.get_data())
        return invalid_request()

    try:
        repo = body["repository"]["clone_url"]
        branch = get_branch_from_ref(body["ref"])
    except KeyError:
        app.logger.warning("Keys missing from request")
        app.logger.debug(body)
        return invalid_request()

    if branch is None:
        app.logger.warning("Could not get branch from ref %s from repo %s", repo, (body or {}).get("ref"))
        return invalid_request("Nothing was pushed to a branch.")

    if not is_branch_in_naucse(repo, branch):
        app.logger.warning("Branch %s from repo %s is not used in naucse.python.cz", branch, repo)
        return invalid_request("The hook was called for a repo/branch combo that's not present in naucse.python.cz")

    commit = get_last_commit_in_branch(repo, branch)

    if not commit:
        app.logger.warning("Could not load the last commit in branch %s in repo %s", branch, repo)
        return invalid_request("Could not load the last commit from GitHub.")
    elif last_commit[repo][branch] == commit:
        app.logger.warning("A build was already triggered for branch %s from repo %s", branch, repo)
        return invalid_request("A build was already triggered for this commit in this branch.")

    last_commit[repo][branch] = commit
    trigger_build(repo, branch)

    app.logger.info("Triggered build of naucse.python.cz, branch %s, repo %s", branch, repo)

    return jsonify({
        "success": "naucse.python.cz build was triggered."
    })


if __name__ == '__main__':
    app.run()
