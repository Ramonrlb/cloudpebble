import requests
import urllib
from datetime import datetime
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from django.core.urlresolvers import reverse
from django.views.decorators.http import require_POST, require_safe
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from ide.api import json_failure, json_response
from ide.models.project import Project
from ide.models.monkey import TestSession, TestRun, TestCode, TestLog
from ide.tasks.monkey import run_test_session
from ide.models.files import TestFile


__author__ = 'joe'


def serialise_run(run, link_test=True, link_session=True):
    result = {
        'id': run.id,
        'name': run.name,
        'logs': reverse('ide:get_test_run_log', args=[run.session.project.id, run.id]) if run.has_log else None,
        'date_added': str(run.session.date_added)
    }
    if link_test and run.has_test:
        result['test'] = {
            'id': run.test.id,
            'name': run.test.file_name
        }
    if link_session:
        result['session_id'] = run.session.id
    if run.code is not None:
        result['code'] = run.code
    if run.date_started is not None:
        result['date_started'] = str(run.date_started)
    if run.date_completed is not None:
        result['date_completed'] = str(run.date_completed)
    return result


def serialise_session(session, include_runs=False):
    runs = TestRun.objects.filter(session=session)
    result = {
        'id': session.id,
        'date_added': str(session.date_added),
        'passes': len(runs.filter(code=TestCode.PASSED)),
        'fails': len(runs.filter(code__lt=0)),
        'run_count': len(runs)
    }
    if session.date_started is not None:
        result['date_started'] = str(session.date_started)
    if session.date_completed is not None:
        result['date_completed'] = str(session.date_completed)
    if include_runs:
        result['runs'] = [serialise_run(run, link_session=False, link_test=True) for run in runs]
    return result


# GET /project/<id>/test_sessions/<session_id>
@require_safe
@login_required
def get_test_session(request, project_id, session_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    session = get_object_or_404(TestSession, pk=session_id, project=project)
    # TODO: KEEN
    return json_response({"data": serialise_session(session)})


# GET /project/<id>/test_sessions?date_from=&date_to=
@require_safe
@login_required
def get_test_sessions(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    id = request.GET.get('id', None)
    kwargs = {'project': project}
    if id is not None:
        kwargs['id'] = id
    sessions = TestSession.objects.filter(**kwargs)
    # TODO: KEEN
    # TODO: deal with errors here on the client
    return json_response({"data": [serialise_session(session) for session in sessions]})



# GET /project/<id>/test_runs/<run_id>
@require_safe
@login_required
def get_test_run(request, project_id, run_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    run = get_object_or_404(TestRun, pk=run_id, session__project=project)
    # TODO: KEEN
    return json_response({"data": serialise_run(run)})


@require_safe
@login_required
def get_test_run_log(request, project_id, run_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    run = get_object_or_404(TestRun, pk=run_id, session__project=project)
    log = get_object_or_404(TestLog, test_run=run)
    contents = log.get_contents()
    return HttpResponse(contents, content_type="text/plain")

# GET /project/<id>/test_runs?test=&session=&date_from=&date_to=
@require_safe
@login_required
def get_test_runs(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    test_id = request.GET.get('test', None)
    session_id = request.GET.get('session', None)
    kwargs = {'session__project': project}
    if test_id is not None:
        kwargs['test__id'] = test_id
    if session_id is not None:
        kwargs['session__id'] = session_id
    runs = TestRun.objects.filter(**kwargs)
    # TODO: KEEN
    return json_response({"data": [serialise_run(run, link_test=True, link_session=True) for run in runs]})

@require_POST
@login_required
def run_qemu_test(request, project_id, test_id):
    # Load request parameters and database objects
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    test = get_object_or_404(TestFile, pk=test_id, project=project)
    token = request.POST['token']
    host = request.POST['host']
    emu = request.POST['emu']
    # Get QEMU server which corresponds to the requested host
    server = next(x for x in set(settings.QEMU_URLS) if host in x)
    # Package the tests
    stream = TestFile.package_tests_to_memory([test])
    # Make the test session and runs
    session, runs = TestSession.setup_test_session(project, [test.id])
    try:
        # TODO: Since we know we're communicating with localhost things, build_absolute_uri may not be appropriate.
        notify = request.build_absolute_uri(reverse('ide:notify_test_session', args=[project_id, session.id]))
        result = requests.post(server + 'qemu/%s/test' % urllib.quote_plus(emu),
                               data={'token': token, 'notify': notify},
                               verify=settings.COMPLETION_CERTS,
                               files=[('archive', ('archive.zip', stream))])

        # Consider doing something to get more meaningful error messages
        result.raise_for_status()
    except Exception as e:
        # If there was an error starting the test, set the ERROR test code
        for run in runs:
            run.code = TestCode.ERROR
            run.log = e.message
            run.save()
        raise e
    response = result.json()
    return json_response(response)


@require_POST
@csrf_exempt
def notify_test_session(request, project_id, session_id):
    project = get_object_or_404(Project, pk=int(project_id))
    session = get_object_or_404(TestSession, pk=int(session_id), project=project)
    token = request.POST['token']
    if token != settings.QEMU_LAUNCH_AUTH_HEADER:
        print "Rejecting test result, posted token %s doesn't match %s" % (request.POST['token'], settings.QEMU_LAUNCH_AUTH_HEADER)
        return json_response({}, status=403)

    log = request.POST['log']
    code = int(request.POST['code'])
    if code == 0:
        result = TestCode.PASSED
    elif code == 1:
        result = TestCode.FAILED
    else:
        result = TestCode.ERROR
    for run in session.runs.all():
        run.code = result
        run.log = log
        run.date_completed = datetime.now()
        run.save()
    return json_response({})


@require_safe
@login_required
def download_tests(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    test_ids = request.GET.get('tests', None)
    if test_ids is None:
        tests = project.test_files.all()
    else:
        ids = [int(test_id) for test_id in test_ids.split(',')]
        tests = TestFile.objects.filter(project=project, id__in=ids)

    stream = TestFile.package_tests_to_memory(tests)
    return HttpResponse(stream, content_type='application/zip')


# POST /project/<id>/test_sessions
@require_POST
@login_required
def post_test_session(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    # We may receive a list of particular tests to run
    # If not, all tests will be run.
    test_ids = request.POST.get('tests', None)
    if test_ids is not None:
        test_ids = [int(test_id) for test_id in test_ids.split(',')]

    # Make the database objects
    session, runs = TestSession.setup_test_session(project, test_ids)
    # TODO: Real implimentation with Liam's infrastructure
    run_test_session.delay(session.id)

    return json_response({"data": serialise_session(session, include_runs=True)})


# TODO: 'ping' functions to see if anything has changed. Or, "changed since" parameters.
