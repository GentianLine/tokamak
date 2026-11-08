"""ProjectRepo model and service layer: attach/detach repos to projects."""

import pytest
from django.core.exceptions import ValidationError

from tokamak import services
from tokamak.models import ProjectRepo
from tests._utils import make_project


def test_add_repo_basic(db):
    project = make_project('R1')
    repo = services.project_add_repo(
        project, name='core', url='https://github.com/org/core.git'
    )
    assert repo.pk is not None
    assert repo.project == project
    assert repo.kind == 'git'
    assert repo.url == 'https://github.com/org/core.git'
    assert not repo.is_primary


def test_add_repo_with_all_fields(db):
    project = make_project('R2')
    repo = services.project_add_repo(
        project,
        name='frontend',
        url='https://gitlab.com/team/frontend.git',
        path='./workspaces/frontend',
        branch='develop',
        description='React SPA with Vite',
        is_primary=True,
    )
    assert repo.is_primary
    assert repo.path == './workspaces/frontend'
    assert repo.branch == 'develop'


def test_add_repo_folder_kind(db):
    project = make_project('R3')
    repo = services.project_add_repo(
        project, name='docs', kind='folder', path='./shared/docs'
    )
    assert repo.kind == 'folder'
    assert repo.url == ''


def test_add_repo_requires_name(db):
    project = make_project('R4')
    with pytest.raises(ValidationError) as exc:
        services.project_add_repo(project, name='')
    assert 'name' in exc.value.message_dict


def test_add_repo_rejects_duplicate_name(db):
    project = make_project('R5')
    services.project_add_repo(project, name='dup', url='https://a.git')
    with pytest.raises(ValidationError) as exc:
        services.project_add_repo(project, name='dup', url='https://b.git')
    assert 'name' in exc.value.message_dict


def test_add_repo_rejects_bad_kind(db):
    project = make_project('R6')
    with pytest.raises(ValidationError) as exc:
        services.project_add_repo(project, name='x', kind='docker')
    assert 'kind' in exc.value.message_dict


def test_is_primary_exclusive(db):
    """Setting a new primary demotes the old one."""
    project = make_project('R7')
    r1 = services.project_add_repo(
        project, name='a', url='https://a.git', is_primary=True
    )
    r2 = services.project_add_repo(
        project, name='b', url='https://b.git', is_primary=True
    )
    r1.refresh_from_db()
    assert not r1.is_primary
    assert r2.is_primary


def test_remove_repo(db):
    project = make_project('R8')
    repo = services.project_add_repo(project, name='gone', url='https://g.git')
    services.project_remove_repo(repo)
    assert not ProjectRepo.objects.filter(pk=repo.pk).exists()


def test_remove_repo_wrong_type(db):
    project = make_project('R9')
    with pytest.raises(ValidationError):
        services.project_remove_repo(project)  # not a ProjectRepo


def test_repos_cascade_on_project_delete(db):
    project = make_project('R10')
    services.project_add_repo(project, name='x', url='https://x.git')
    assert ProjectRepo.objects.filter(project=project).count() == 1
    project.delete()
    assert ProjectRepo.objects.count() == 0


def test_repo_ordering(db):
    """Primary repos sort first, then by name."""
    project = make_project('R11')
    services.project_add_repo(project, name='zeta', url='https://z.git')
    services.project_add_repo(
        project, name='alpha', url='https://a.git', is_primary=True
    )
    repos = list(project.repos.all())
    assert repos[0].name == 'alpha'  # primary first
    assert repos[1].name == 'zeta'


def test_str_representation(db):
    project = make_project('R12')
    repo = services.project_add_repo(
        project, name='core', url='https://github.com/org/core.git'
    )
    s = str(repo)
    assert 'core' in s
    assert 'git' in s
    assert 'https://github.com/org/core.git' in s
