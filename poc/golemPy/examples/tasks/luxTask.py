import tempfile
import os
import sys
import glob
import cPickle as pickle
import zlib
import zipfile
import subprocess
import psutil
import shutil

def formatLuxRendererCmd(cmdFile, startTask, output_file, outfilebasename, scenefile, numThreads):
    cmd = ["{}".format(cmdFile), "{}".format(scenefile), "-o",
           "{}/{}{}.png".format(output_file, outfilebasename, startTask), "-t", "{}".format(numThreads) ]
    print cmd
    return cmd

def __readFromEnvironment():
    GOLEM_ENV = 'GOLEM'
    path = os.environ.get(GOLEM_ENV)
    if not path:
        print "No Golem environment variable found... Assuming that exec is in working folder"
        if is_windows():
            return 'luxconsole.exe'
        else:
            return 'luxconsole'

    sys.path.append(path)

    from examples.gnr.RenderingEnvironment import LuxRenderEnvironment
    env = LuxRenderEnvironment()
    cmdFile = env.getLuxConsole()
    if cmdFile:
        return cmdFile
    else:
        print "Environment not supported... Assuming that exec is in working folder"
        if is_windows():
            return 'luxconsole.exe'
        else:
            return 'luxconsole'

############################
def returnData(files):
    res = []
    for f in files:
        with open(f, "rb") as fh:
            file_data = fh.read()
        file_data = zlib.compress(file_data, 9)
        res.append(pickle.dumps((os.path.basename(f), file_data)))

    return { 'data': res, 'resultType': 0 }

############################
def returnFiles(files):
    copyPath = os.path.normpath(os.path.join(tmpPath, ".."))
    for f in files:
        shutil.copy2(f, copyPath)

    files = [ os.path.normpath(os.path.join(copyPath, os.path.basename(f))) for f in files]
    return {'data': files, 'resultType': 1 }


############################
def is_windows():
    return sys.platform == 'win32'

def exec_cmd(cmd, nice=20):
    pc = subprocess.Popen(cmd)
    if is_windows():
        import win32process
        win32process.SetPriorityClass(pc._handle, win32process.IDLE_PRIORITY_CLASS)
    else:
        p = psutil.Process(pc.pid)
        p.nice(nice)

    pc.wait()

def makeTmpFile(sceneDir, sceneSrc):
    if is_windows():
        tmpSceneFile = tempfile.TemporaryFile(suffix = ".lxs", dir = sceneDir)
        tmpSceneFile.close()
        f = open(tmpSceneFile.name, 'w')
        f.write(sceneSrc)
        f.close()

        return tmpSceneFile.name
    else:
        tmpSceneFile = os.path.join(sceneDir, "tmpSceneFile.lxs")
        f = open(tmpSceneFile, "w")
        f.write(sceneSrc)
        f.close()
        return tmpSceneFile


############################
def runLuxRendererTask(startTask, outfilebasename, sceneFileSrc, sceneDir, num_cores, ownBinaries, luxConsole):
    print 'LuxRenderer Task'

    output_files = tmpPath

    files = glob.glob(output_files + "/*.png") + glob.glob(output_files + "/*.flm")

    for f in files:
        os.remove(f)

    sceneDir = os.path.normpath(os.path.join(os.getcwd(), sceneDir))
    tmpSceneFile = makeTmpFile(sceneDir, sceneFileSrc)

    if ownBinaries:
        cmdFile = luxConsole
    else:
        cmdFile = __readFromEnvironment()
    if os.path.exists(tmpSceneFile):
        print tmpSceneFile
        cmd = formatLuxRendererCmd(cmdFile, startTask, output_files, outfilebasename, tmpSceneFile, numThreads)
    else:
         print "Scene file does not exist"
         return {'data': [], 'resultType': 0 }

    prevDir = os.getcwd()
    os.chdir(sceneDir)

    exec_cmd(cmd)

    os.chdir(prevDir)
    files = glob.glob(output_files + "/*.png") + glob.glob(output_files + "/*.flm")

    return returnFiles(files)


output = runLuxRendererTask (startTask, outfilebasename, sceneFileSrc, sceneDir, numThreads, ownBinaries, luxConsole)
