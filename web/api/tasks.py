"""
Tasks endpoints for Seny.

Tasks management API for creating, reading, updating, and managing tasks:
- GET /api/tasks - List tasks (with filters)
- GET /api/tasks/today - Due today
- GET /api/tasks/overdue - Overdue tasks
- GET /api/tasks/upcoming - Due in next N days
- GET /api/tasks/categories - List categories
- GET /api/tasks/projects - List projects
- GET /api/tasks/{id} - Get single task
- POST /api/tasks - Create task
- PUT /api/tasks/{id} - Update task
- DELETE /api/tasks/{id} - Delete task
- POST /api/tasks/{id}/complete - Mark complete
- POST /api/tasks/{id}/reopen - Reopen completed task
- POST /api/tasks/{id}/reminder - Add reminder
"""

import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.tasks_service import TasksService

logger = logging.getLogger(__name__)


# Create tasks router
router = APIRouter()


# Request/Response models
class TaskCreate(BaseModel):
    """Request model for creating a task."""
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "medium"
    due_date: Optional[str] = None  # ISO format
    category: Optional[str] = None
    project: Optional[str] = None


class TaskUpdate(BaseModel):
    """Request model for updating a task."""
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    category: Optional[str] = None
    project: Optional[str] = None
    status: Optional[str] = None


class ReminderCreate(BaseModel):
    """Request model for adding a reminder."""
    remind_at: str  # ISO format


class TaskSummary(BaseModel):
    """Task summary for list view."""
    id: int
    title: str
    description: Optional[str]
    status: str
    priority: str
    due_date: Optional[str]
    category: Optional[str]
    project: Optional[str]
    is_recurring: bool
    type: str = "task"  # "task" or "errand"
    created_at: str
    updated_at: str


class TaskDetail(BaseModel):
    """Full task details."""
    id: int
    title: str
    description: Optional[str]
    status: str
    priority: str
    due_date: Optional[str]
    completed_at: Optional[str]
    category: Optional[str]
    project: Optional[str]
    is_recurring: bool
    recurrence_pattern: Optional[str]
    recurrence_interval: Optional[int]
    recurrence_end_date: Optional[str]
    parent_task_id: Optional[int]
    created_at: str
    updated_at: str


class TasksListResponse(BaseModel):
    """Response for list tasks endpoint."""
    tasks: list[TaskSummary]
    total: int


class CategoryInfo(BaseModel):
    """Category with count."""
    category: str
    count: int


class CategoriesResponse(BaseModel):
    """Response for list categories endpoint."""
    categories: list[CategoryInfo]


class ProjectInfo(BaseModel):
    """Project with count."""
    project: str
    count: int


class ProjectsResponse(BaseModel):
    """Response for list projects endpoint."""
    projects: list[ProjectInfo]


class ReminderInfo(BaseModel):
    """Reminder details."""
    id: int
    remind_at: str
    reminder_type: str
    is_sent: bool


def _task_to_summary(task: dict) -> TaskSummary:
    """Convert task dict to summary model."""
    return TaskSummary(
        id=task["id"],
        title=task["title"],
        description=task.get("description"),
        status=task["status"],
        priority=task["priority"],
        due_date=task.get("due_date"),
        category=task.get("category"),
        project=task.get("project"),
        is_recurring=task.get("is_recurring", False),
        type=task.get("type", "task"),
        created_at=task["created_at"],
        updated_at=task["updated_at"]
    )


def _task_to_detail(task: dict) -> TaskDetail:
    """Convert task dict to detail model."""
    return TaskDetail(
        id=task["id"],
        title=task["title"],
        description=task.get("description"),
        status=task["status"],
        priority=task["priority"],
        due_date=task.get("due_date"),
        completed_at=task.get("completed_at"),
        category=task.get("category"),
        project=task.get("project"),
        is_recurring=task.get("is_recurring", False),
        recurrence_pattern=task.get("recurrence_pattern"),
        recurrence_interval=task.get("recurrence_interval"),
        recurrence_end_date=task.get("recurrence_end_date"),
        parent_task_id=task.get("parent_task_id"),
        created_at=task["created_at"],
        updated_at=task["updated_at"]
    )


@router.get("", response_model=TasksListResponse)
async def list_tasks(
    user_id: str = Depends(require_auth),
    task_type: Optional[str] = Query(None, description="Filter by type: 'task' or 'errand'"),
    status: Optional[str] = Query(None, description="Filter by status"),
    priority: Optional[str] = Query(None, description="Filter by priority"),
    category: Optional[str] = Query(None, description="Filter by category"),
    project: Optional[str] = Query(None, description="Filter by project"),
    include_completed: bool = Query(False, description="Include completed tasks"),
    limit: int = Query(50, ge=1, le=100, description="Max tasks to return")
):
    """
    List tasks for the authenticated user.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of tasks, ordered by due date and priority
    """
    tasks_service = TasksService(int(user_id))
    tasks = await tasks_service.list_tasks(
        task_type=task_type,
        status=status,
        priority=priority,
        category=category,
        project=project,
        include_completed=include_completed,
        limit=limit
    )

    return TasksListResponse(
        tasks=[_task_to_summary(t) for t in tasks],
        total=len(tasks)
    )


@router.get("/today", response_model=TasksListResponse)
async def get_today_tasks(user_id: str = Depends(require_auth)):
    """
    Get tasks due today.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of tasks due today
    """
    tasks_service = TasksService(int(user_id))
    tasks = await tasks_service.get_due_today()

    return TasksListResponse(
        tasks=[_task_to_summary(t) for t in tasks],
        total=len(tasks)
    )


@router.get("/overdue", response_model=TasksListResponse)
async def get_overdue_tasks(user_id: str = Depends(require_auth)):
    """
    Get overdue tasks.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of overdue tasks
    """
    tasks_service = TasksService(int(user_id))
    tasks = await tasks_service.get_overdue()

    return TasksListResponse(
        tasks=[_task_to_summary(t) for t in tasks],
        total=len(tasks)
    )


@router.get("/upcoming", response_model=TasksListResponse)
async def get_upcoming_tasks(
    user_id: str = Depends(require_auth),
    days: int = Query(7, ge=1, le=30, description="Days to look ahead")
):
    """
    Get upcoming tasks.

    Protected endpoint - requires valid JWT token.

    Args:
        days: Number of days to look ahead (default 7)

    Returns:
        List of upcoming tasks
    """
    tasks_service = TasksService(int(user_id))
    tasks = await tasks_service.get_upcoming(days=days)

    return TasksListResponse(
        tasks=[_task_to_summary(t) for t in tasks],
        total=len(tasks)
    )


@router.get("/categories", response_model=CategoriesResponse)
async def list_categories(user_id: str = Depends(require_auth)):
    """
    List all categories with task counts.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of categories with counts
    """
    tasks_service = TasksService(int(user_id))
    categories = await tasks_service.list_categories()

    return CategoriesResponse(
        categories=[CategoryInfo(**c) for c in categories]
    )


@router.get("/projects", response_model=ProjectsResponse)
async def list_projects(user_id: str = Depends(require_auth)):
    """
    List all projects with task counts.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of projects with counts
    """
    tasks_service = TasksService(int(user_id))
    projects = await tasks_service.list_projects()

    return ProjectsResponse(
        projects=[ProjectInfo(**p) for p in projects]
    )


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Get a specific task.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID

    Returns:
        Full task details
    """
    tasks_service = TasksService(int(user_id))
    task = await tasks_service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    return _task_to_detail(task)


@router.post("", response_model=TaskDetail, status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate,
    user_id: str = Depends(require_auth)
):
    """
    Create a new task.

    Protected endpoint - requires valid JWT token.

    Returns:
        The created task
    """
    tasks_service = TasksService(int(user_id))

    # Parse due date if provided
    due_date = None
    if task.due_date:
        try:
            due_date = datetime.fromisoformat(task.due_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid due_date format: {task.due_date}. Use ISO format."
            )

    created_task = await tasks_service.create_task(
        title=task.title,
        description=task.description,
        priority=task.priority or "medium",
        due_date=due_date,
        category=task.category,
        project=task.project
    )

    if not created_task:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create task"
        )

    return _task_to_detail(created_task)


@router.put("/{task_id}", response_model=TaskDetail)
async def update_task(
    task_id: int,
    task: TaskUpdate,
    user_id: str = Depends(require_auth)
):
    """
    Update an existing task.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID
        task: Fields to update

    Returns:
        The updated task
    """
    tasks_service = TasksService(int(user_id))

    # Check task exists
    existing = await tasks_service.get_task(task_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    # Parse due date if provided
    due_date = None
    if task.due_date:
        try:
            due_date = datetime.fromisoformat(task.due_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid due_date format: {task.due_date}. Use ISO format."
            )

    updated_task = await tasks_service.update_task(
        task_id=task_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        due_date=due_date,
        category=task.category,
        project=task.project,
        status=task.status
    )

    if not updated_task:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update task"
        )

    return _task_to_detail(updated_task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Delete a task.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID

    Returns:
        204 No Content on success
    """
    tasks_service = TasksService(int(user_id))

    # Check task exists
    existing = await tasks_service.get_task(task_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    success = await tasks_service.delete_task(task_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete task"
        )

    return None


@router.post("/{task_id}/complete", response_model=TaskDetail)
async def complete_task(
    task_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Mark a task as completed.

    For recurring tasks, this also generates the next occurrence.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID

    Returns:
        The completed task
    """
    tasks_service = TasksService(int(user_id))

    task = await tasks_service.complete_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    return _task_to_detail(task)


@router.post("/{task_id}/reopen", response_model=TaskDetail)
async def reopen_task(
    task_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Reopen a completed or cancelled task.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID

    Returns:
        The reopened task
    """
    tasks_service = TasksService(int(user_id))

    task = await tasks_service.reopen_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    return _task_to_detail(task)


@router.post("/{task_id}/reminder", response_model=ReminderInfo, status_code=status.HTTP_201_CREATED)
async def add_reminder(
    task_id: int,
    reminder: ReminderCreate,
    user_id: str = Depends(require_auth)
):
    """
    Add a reminder to a task.

    Protected endpoint - requires valid JWT token.

    Args:
        task_id: The task's ID
        reminder: Reminder details

    Returns:
        The created reminder
    """
    tasks_service = TasksService(int(user_id))

    # Parse remind_at
    try:
        remind_at = datetime.fromisoformat(reminder.remind_at.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid remind_at format: {reminder.remind_at}. Use ISO format."
        )

    result = await tasks_service.add_reminder(task_id, remind_at)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    return ReminderInfo(
        id=result["id"],
        remind_at=result["remind_at"],
        reminder_type=result["reminder_type"],
        is_sent=result["is_sent"]
    )
