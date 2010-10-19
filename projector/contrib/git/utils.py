import copy

from django.http import HttpResponse

def is_git_request(request):
    """
    Returns True if request was made by git user agent, False otherwise.
    """
    agent = request.META.get('HTTP_USER_AGENT', None)
    try:
        return agent.split('/')[0] == 'git'
    except IndexError:
        return False
    return False

def start_response2django(status, headers):
    """
    Fakes standard wsgi start_response function but returns instance of
    ``django.http.HttpResponse`` object.
    """
    response = HttpResponse()
    response.status_code = int(status[:3])
    for key, val in headers:
        print key, val
        response[key] = val
    return response.write


class GitResponse(HttpResponse):

    def write(self, content):
        if hasattr(content, '__iter__'):
            for chunk in content:
                self.write(chunk)
        else:
            super(GitResponse, self).write(content)


def get_wsgi_response(app, request):
    """
    Returns ``django.http.HttpResponse`` object from WSGI applcation.
    """
    response = GitResponse()
    def start_response(status, headers):
        response.status_code = int(status[:3])
        for key, val in headers:
            logging.info("%s: %s" % (key, val))
            response[key] = val
        return response.write
    env = copy.copy(request.META)
    response.write(app(env, start_response))
    return response

