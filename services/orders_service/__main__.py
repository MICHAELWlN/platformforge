from orders_service import SERVICE_NAME
from pf_service_common.server import serve

if __name__ == "__main__":
    serve(SERVICE_NAME, "orders_service.app:create_app")
