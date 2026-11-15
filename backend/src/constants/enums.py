from enum import Enum
class Role(str,Enum):
    admin="admin"
    analyst="analyst"
    viewer="viewer"
class ReviewStatus(str,Enum):
    queued="queued"
    running="running"
    completed="completed"
    approved="approved"
    failed="failed"
class AttemptStatus(str,Enum):
    running="running"
    completed="completed"
    failed="failed"
    abandoned="abandoned"
class AttemptPhase(str,Enum):
    claimed="claimed"
    computing="computing"
    writing="writing"
    verifying="verifying"
    published="published"
    failed="failed"
    abandoned="abandoned"
LINES=["一线","二线","三线"]
