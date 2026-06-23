import http.server
import socketserver


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    api_handler = None



def make_handler(api, handler_factory):
    return handler_factory(api)



def run_server(port, api_handler, handler_factory):
    httpd = Server(("0.0.0.0", port), make_handler(api_handler, handler_factory))
    httpd.api_handler = api_handler
    return httpd
