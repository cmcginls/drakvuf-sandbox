import json
import os
import re
import pathlib
from tempfile import NamedTemporaryFile

import requests
import logging

from flask import Flask, jsonify, request, send_file, redirect, send_from_directory, Response, abort
from karton2 import Config, Producer, Task, Resource
from minio.error import NoSuchKey

from drakcore.system import SystemService
from drakcore.util import get_config
from drakcore.analysis import AnalysisProxy
from drakcore.database import Database


app = Flask(__name__, static_folder='frontend/build/static')
conf = get_config()

rs = SystemService(conf).rs
minio = SystemService(conf).minio
db = Database(conf.config["drakmon"].get("database", "sqlite:///var/lib/drakcore/drakcore.db"),
              pathlib.Path(__file__).parent / "migrations")


@app.errorhandler(NoSuchKey)
def resource_not_found(e):
    return jsonify(error="Object not found"), 404


@app.after_request
def add_header(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Range'
    return response


@app.route("/upload", methods=['POST'])
def upload():
    producer = Producer(conf)

    with NamedTemporaryFile() as f:
        request.files['file'].save(f.name)

        with open(f.name, "rb") as fr:
            sample = Resource("sample", fr.read())

    task = Task({"type": "sample", "stage": "recognized", "platform": "win32"})
    task.add_payload("override_uid", task.uid)

    # Add analysis timeout to task
    timeout = request.form.get("timeout")
    if timeout:
        task.add_payload("timeout", int(timeout))

    # Add filename override to task
    if request.form.get("file_name"):
        filename = request.form.get("file_name")
    else:
        filename = request.files['file'].filename
    if not re.fullmatch(r'^((?![\\/><|:&])[\x20-\xfe])+\.(?:dll|exe|doc|docm|docx|dotm|xls|xlsx|xlsm|xltx|xltm)$',
                        filename, flags=re.IGNORECASE):
        return jsonify({"error": "invalid file_name"}), 400
    task.add_payload("file_name", os.path.splitext(filename)[0])

    # Extract and add extension
    extension = os.path.splitext(filename)[1][1:]
    if extension:
        task.headers['extension'] = extension

    # Add startup command to task
    start_command = request.form.get("start_command")
    if start_command:
        task.add_payload("start_command", start_command)

    task.add_resource("sample", sample)

    producer.send_task(task)

    return jsonify({"task_uid": task.uid})


def get_analysis_metadata(analysis_uid):
    db_result = db.select_metadata_by_uid(analysis_uid)
    if db_result is not None:
        return db_result

    # Cache miss, have to ask MinIO
    analysis = AnalysisProxy(minio, analysis_uid)
    metadata = analysis.get_metadata()

    try:
        db.insert_metadata(analysis_uid, metadata)
    except Exception:
        app.logger.exception("Failed to insert %s metadata", analysis_uid)

    return metadata


@app.route("/list")
def route_list():
    analyses = []
    for analysis in AnalysisProxy(minio, None).enumerate():
        try:
            analyses.append({"id": analysis.uid, "meta": get_analysis_metadata(analysis.uid)})
        except NoSuchKey:
            continue

    def sorting_key(o):
        return o.get('meta', {}).get('time_finished', -1)
    return jsonify(sorted(analyses, key=sorting_key, reverse=True))


@app.route("/processed/<task_uid>/<which>")
def processed(task_uid, which):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as f:
        analysis.get_processed(f, which)
        return send_file(f.name, mimetype='application/json')


@app.route("/processed/<task_uid>/apicall/<pid>")
def apicall(task_uid, pid):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as f:
        analysis.get_apicalls(f, pid)
        return send_file(f.name)


@app.route("/logs/<task_uid>/<log_type>")
def logs(task_uid, log_type):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as f:
        # Copy Range header if it exists
        headers = {}
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        analysis.get_log(log_type, f, headers=headers)
        return send_file(f.name, mimetype='text/plain')


@app.route("/logindex/<task_uid>/<log_type>")
def logindex(task_uid, log_type):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as f:
        analysis.get_log_index(log_type, f)
        return send_file(f.name)


@app.route("/dumps/<task_uid>")
def dumps(task_uid):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as tmp:
        analysis.get_dumps(tmp)
        return send_file(tmp.name, mimetype='application/zip')


@app.route("/logs/<task_uid>")
def list_logs(task_uid):
    analysis = AnalysisProxy(minio, task_uid)
    return jsonify(list(analysis.list_logs()))


@app.route("/graph/<task_uid>")
def graph(task_uid):
    analysis = AnalysisProxy(minio, task_uid)
    with NamedTemporaryFile() as tmp:
        analysis.get_graph(tmp)
        return send_file(tmp.name, mimetype='text/plain')


@app.route("/metadata/<task_uid>")
def metadata(task_uid):
    return jsonify(get_analysis_metadata(task_uid))


@app.route("/status/<task_uid>")
def status(task_uid):
    tasks = rs.keys("karton.task:*")
    res = {"status": "done"}

    for task_key in tasks:
        task = json.loads(rs.get(task_key))

        if task["root_uid"] == task_uid:
            if task["status"] != "Finished":
                res["status"] = "pending"
                break

    res["vm_id"] = rs.get(f"drakvnc:{task_uid}")
    return jsonify(res)


@app.route("/")
def index():
    return send_file("frontend/build/index.html")


@app.route("/robots.txt")
def robots():
    return send_file("frontend/build/robots.txt")


@app.route('/assets/<path:path>')
def send_assets(path):
    return send_from_directory('frontend/build/assets', path)


@app.route("/<path:path>")
def catchall(path):
    return send_file("frontend/build/index.html")


def main():
    drakmon_cfg = {k: v for k, v in conf.config.items("drakmon")}
    app.run(host=drakmon_cfg["listen_host"], port=drakmon_cfg["listen_port"])


if __name__ == "__main__":
    main()
