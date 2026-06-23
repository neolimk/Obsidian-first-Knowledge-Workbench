import json


def build_sse_event(payload):
    return "data: {}\n\n".format(json.dumps(payload, ensure_ascii=False)).encode("utf-8")



def build_done_event():
    return b"data: [DONE]\n\n"
