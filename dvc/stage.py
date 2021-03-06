import os
import stat
import yaml
import itertools

from dvc.system import System
from dvc.data_cloud import file_md5
from dvc.exceptions import DvcException
from dvc.executor import Executor


class OutputError(DvcException):
    def __init__(self, path, msg):
        super(OutputError, self).__init__('Output file \'{}\' error: {}'.format(path, msg))


class OutputNoCacheError(OutputError):
    def __init__(self, path):
        super(OutputNoCacheError, self).__init__(path, 'no cache')


class OutputOutsideOfRepoError(OutputError):
    def __init__(self, path):
        super(OutputOutsideOfRepoError, self).__init__(path, 'outside of repository')


class OutputDoesNotExistError(OutputError):
    def __init__(self, path):
        super(OutputDoesNotExistError, self).__init__(path, 'does not exist')


class OutputIsNotFileError(OutputError):
    def __init__(self, path):
        super(OutputIsNotFileError, self).__init__(path, 'not a file')


class OutputAlreadyTrackedError(OutputError):
    def __init__(self, path):
        super(OutputAlreadyTrackedError, self).__init__(path, 'already tracked by scm(e.g. git)')


class Dependency(object):
    PARAM_PATH = 'path'
    PARAM_MD5 = 'md5'

    def __init__(self, project, path, md5=None):
        self.project = project
        self.path = os.path.abspath(os.path.realpath(path))

        if not self.path.startswith(self.project.root_dir):
            raise OutputOutsideOfRepoError(self.path)

        self.md5 = md5

    @property
    def dvc_path(self):
        return os.path.relpath(self.path, self.project.root_dir)

    def _changed_md5(self):
        if not os.path.exists(self.path):
            return True

        state = self.project.state.get(self.path)
        if state and state.mtime == self.mtime():
            md5 = state.md5
        else:
            md5 = file_md5(self.path)[0]

        return self.md5 != md5

    def changed(self):
        return self._changed_md5()

    def mtime(self):
        return os.path.getmtime(self.path)

    def update(self):
        if not os.path.exists(self.path):
            raise OutputDoesNotExistError(self.path)
        if not os.path.isfile(self.path):
            raise OutputIsNotFileError(self.path)

        state = self.project.state.get(self.path)
        if state and state.mtime == self.mtime():
            md5 = state.md5
            msg = '{} using md5 {} from state file'
            self.project.logger.debug(msg.format(self.path, md5))
            self.md5 = md5
        else:
            self.md5 = file_md5(self.path)[0]
            self.project.state.update(self.path, self.md5, self.mtime())

    def dumpd(self, cwd):
        return {
            Output.PARAM_PATH: os.path.relpath(self.path, cwd),
            Output.PARAM_MD5: self.md5,
        }

    @classmethod
    def loadd(cls, project, d, cwd=os.curdir):
        path = os.path.join(cwd, d[Output.PARAM_PATH])
        md5 = d.get(Output.PARAM_MD5, None)
        return cls(project, path, md5=md5)

    @classmethod
    def loadd_from(cls, project, d_list, cwd=os.curdir):
        return [cls.loadd(project, x, cwd=cwd) for x in d_list]

    @classmethod
    def loads(cls, project, s, cwd=os.curdir):
        return cls(project, os.path.join(cwd, s), md5=None)

    @classmethod
    def loads_from(cls, project, s_list, cwd=os.curdir):
        return [cls.loads(project, x, cwd=cwd) for x in s_list]

    def stage(self):
        for stage in self.project.stages():
            for out in stage.outs:
                if self.path == out.path:
                    return stage
        return None


class Output(Dependency):
    PARAM_CACHE = 'cache'

    def __init__(self, project, path, md5=None, use_cache=True):
        super(Output, self).__init__(project, path, md5=md5)
        self.use_cache = use_cache

    @property
    def cache(self):
        return self.project.cache.get(self.md5)

    def dumpd(self, cwd):
        ret = super(Output, self).dumpd(cwd)
        ret[Output.PARAM_CACHE] = self.use_cache
        return ret

    @classmethod
    def loadd(cls, project, d, cwd=os.curdir):
        ret = super(Output, cls).loadd(project, d, cwd=cwd)
        ret.use_cache = d.get(Output.PARAM_CACHE, True)
        return ret

    @classmethod
    def loads(cls, project, s, use_cache=True, cwd=os.curdir):
        ret = super(Output, cls).loads(project, s, cwd=cwd)
        ret.use_cache = use_cache
        return ret

    @classmethod
    def loads_from(cls, project, s_list, use_cache=False, cwd=os.curdir):
        return [cls.loads(project, x, use_cache=use_cache, cwd=cwd) for x in s_list]

    def changed(self):
        if not self.use_cache:
            return super(Output, self).changed()

        return os.path.exists(self.path) \
               and os.path.exists(self.cache) \
               and not System.samefile(self.path, self.cache)

    def link(self, checkout=False):
        if not self.use_cache:
            raise OutputNoCacheError(self.path)

        if not os.path.exists(self.path) and not os.path.exists(self.cache):
            raise OutputNoCacheError(self.path)

        if os.path.exists(self.path) and \
           os.path.exists(self.cache) and \
           System.samefile(self.path, self.cache) and \
           os.stat(self.cache).st_mode & stat.S_IREAD:
            return

        if os.path.exists(self.cache):
            if os.path.exists(self.path):
                # This means that we already have cache for this data.
                # We remove data and link it to existing cache to save
                # some space.
                self.remove()
            src = self.cache
            link = self.path
        elif not checkout:
            src = self.path
            link = self.cache
        else:
            raise OutputNoCacheError(self.path)

        System.hardlink(src, link)

        os.chmod(self.path, stat.S_IREAD)

    def checkout(self):
        if not self.use_cache:
            return
        if not os.path.exists(self.cache):
            self.project.logger.warn(u'\'{}\': cache file not found'.format(self.dvc_path))
            self.remove()
        else:
            self.link(checkout=True)

    def save(self):
        if not self.use_cache:
            return

        if self.project.scm.is_tracked(self.path):
            raise OutputAlreadyTrackedError(self.path)

        self.project.scm.ignore(self.path)
        self.link()

    def remove(self):
        if not os.path.exists(self.path):
            return

        self.project.logger.debug("Removing '{}'".format(self.path))
        os.chmod(self.path, stat.S_IWUSR)
        os.unlink(self.path)
        if os.path.exists(self.cache):
            os.chmod(self.cache, stat.S_IREAD)


class Stage(object):
    STAGE_FILE = 'Dvcfile'
    STAGE_FILE_SUFFIX = '.dvc'

    PARAM_CMD = 'cmd'
    PARAM_DEPS = 'deps'
    PARAM_OUTS = 'outs'
    PARAM_LOCKED = 'locked'

    def __init__(self, project, path=None, cmd=None, cwd=None, deps=[], outs=[], locked=False):
        self.project = project
        self.path = path
        self.cmd = cmd
        self.cwd = cwd
        self.outs = outs
        self.deps = deps
        self.locked = locked

    @property
    def relpath(self):
        return os.path.relpath(self.path)

    @property
    def dvc_path(self):
        return os.path.relpath(self.path, self.project.root_dir)

    @staticmethod
    def is_stage_file(path):
        if not os.path.isfile(path):
            return False

        if not path.endswith(Stage.STAGE_FILE_SUFFIX) and os.path.basename(path) != Stage.STAGE_FILE:
            return False

        return True

    def changed(self):
        for entry in itertools.chain(self.outs, self.deps):
            if entry.changed():
                self.project.logger.debug("{} changed".format(self.path))
                return True
        return False

    def remove_outs(self):
        for out in self.outs:
            out.remove()

    def remove(self):
        self.remove_outs()
        os.unlink(self.path)

    def reproduce(self, force=False):
        if not self.changed() and not force:
            return

        if self.cmd:
            # Removing outputs only if we actually have command to reproduce
            self.remove_outs()

        self.project.logger.info("Reproducing {}:\n\t{}".format(self.relpath, self.cmd))
        self.run()
        self.project.logger.debug("{} was reproduced".format(self.relpath))

    @staticmethod
    def loadd(project, d, path):
        path = os.path.abspath(path)
        cwd = os.path.dirname(path)
        cmd = d[Stage.PARAM_CMD]
        deps = Dependency.loadd_from(project, d[Stage.PARAM_DEPS], cwd=cwd)
        outs = Output.loadd_from(project, d[Stage.PARAM_OUTS], cwd=cwd)
        locked = d[Stage.PARAM_LOCKED]

        return Stage(project=project,
                     path=path,
                     cmd=cmd,
                     cwd=cwd,
                     deps=deps,
                     outs=outs,
                     locked=locked)

    @staticmethod
    def load(project, fname):
        with open(fname, 'r') as fd:
            return Stage.loadd(project, yaml.safe_load(fd), fname)

    def dumpd(self):
        deps = [x.dumpd(self.cwd) for x in self.deps]
        outs = [x.dumpd(self.cwd) for x in self.outs]

        return {
            Stage.PARAM_CMD: self.cmd,
            Stage.PARAM_DEPS: deps,
            Stage.PARAM_OUTS: outs,
            Stage.PARAM_LOCKED: self.locked
        }

    def dump(self, fname=None):
        if not fname:
            fname = self.path

        with open(fname, 'w') as fd:
            yaml.safe_dump(self.dumpd(), fd, default_flow_style=False)

    def save(self):
        for dep in self.deps:
            dep.update()

        for out in self.outs:
            out.update()
            out.save()

    def run(self):
        if self.cmd:
            Executor.exec_cmd_only_success(self.cmd, cwd=str(self.cwd), shell=True)
        self.save()

    def checkout(self):
        for out in self.outs:
            out.checkout()
