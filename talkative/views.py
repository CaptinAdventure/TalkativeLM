from urllib.parse import parse_qs

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .board import get_board


@require_http_methods(["GET", "POST"])
def mailbox(request):
    board = get_board()
    if request.method == "POST":
        try:
            length = int(request.headers.get("Content-Length", ""))
            if not 0 <= length <= 16384:
                raise ValueError("Invalid request size")
            if request.content_type != "application/x-www-form-urlencoded":
                raise ValueError("Expected a form submission")
            fields = parse_qs(request.body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)

            def field(name):
                values = fields.get(name, [""])
                if len(values) != 1:
                    raise ValueError("Duplicate form field")
                return values[0]

            reply = field("reply_to")
            if reply and (not reply.isascii() or not reply.isdigit()):
                raise ValueError("Reply To must be a positive message ID")
            board.create_message(field("sender"), field("subject"), field("body"), int(reply) if reply else None)
        except (ValueError, UnicodeError) as exc:
            return HttpResponse(str(exc), status=400, content_type="text/plain; charset=utf-8")
        return HttpResponse(status=303, headers={"Location": "/"})

    reply_to = request.GET.get("reply_to", "")
    reply_to = reply_to if reply_to.isascii() and reply_to.isdigit() else ""
    messages = board.messages()
    readers = board.readers()
    agents = board.agents()
    for message in messages:
        message["read_by"] = readers.get(message["id"], [])
    return render(request, "talkative/mailbox.html", {
        "messages": reversed(messages),
        "message_count": len(messages),
        "agents": agents,
        "online_count": sum(agent["online"] for agent in agents),
        "reply_to": reply_to,
    })


@require_http_methods(["POST"])
def remove_agent(request, nickname):
    if not get_board().remove_agents([nickname]):
        raise Http404("Unknown agent")
    return HttpResponse(status=303, headers={"Location": "/"})
