from django.urls import path

from .memory_views import memory_note, memory_recall, memory_status
from .reorder_views import reorder_ticket_view
from .sessions_views import (
    session_close,
    session_list,
    session_open,
    session_snapshot,
)
from .views import build_api_router

router = build_api_router()

urlpatterns = list(router.urls) + [
    # clm-mem bridge (tokamak/memory.py): status, search/reinject, write notes
    path('memory/status/', memory_status, name='memory-status'),
    path('memory/recall/', memory_recall, name='memory-recall'),
    path('memory/note/', memory_note, name='memory-note'),
    # ticket memory sessions (tokamak/services.py session lifecycle)
    path('sessions/', session_list, name='session-list'),
    path('sessions/open/', session_open, name='session-open'),
    path('sessions/<int:pk>/close/', session_close, name='session-close'),
    path('sessions/<int:pk>/snapshot/', session_snapshot, name='session-snapshot'),
    # drag-reorder within a display group (tokamak/reorder.py)
    path('reorder/', reorder_ticket_view, name='reorder-ticket'),
]
