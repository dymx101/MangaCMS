

import UniversalArchiveInterface
import os
import os.path
import logging
import rpyc
import magic
import shutil

import hashlib

PHASH_DISTANCE_THRESHOLD = 2


class DbBase(object):
	def __init__(self):
		self.remote = rpyc.connect("localhost", 12345)
		self.db = self.remote.root.DbApi()
		self.hash = self.remote.root.RemoteHasher()


	def convertDbIdToPath(self, inId):
		return self.db.getItems(wantCols=['fsPath', "internalPath"], dbId=inId).pop()

# todo: Have a class for managing search results, which contains all the search-relevant info?
class ArchChecker(DbBase):

	def __init__(self, archPath, pathFilter=['']):
		super().__init__()

		# pathFilter filters
		# Basically, if you pass a list of valid path prefixes, any matches not
		# on any of those path prefixes are not matched.
		# Default is [''], which matches every path, because "anything".startswith('') is true
		self.maskedPaths = pathFilter

		self.archPath    = archPath
		self.arch        = UniversalArchiveInterface.ArchiveReader(archPath)

		self.log = logging.getLogger("Main.Deduper")
		self.log.info("ArchChecker Instantiated")

	# If getMatchingArchives returns something, it means we're /not/ unique,
	# because getMatchingArchives returns matching files
	def isBinaryUnique(self):
		ret = self.getMatchingArchives()

		if len(ret):
			return False
		return True

	def isPhashUnique(self, searchDistance=PHASH_DISTANCE_THRESHOLD):
		ret = self.getPhashMatchingArchives(searchDistance)

		if len(ret):
			return False
		return True


	def getBestBinaryMatch(self):
		ret = self.getMatchingArchives()
		return self._getBestMatchingArchive(ret)

	def getBestPhashMatch(self, distance=PHASH_DISTANCE_THRESHOLD):
		ret = self.getPhashMatchingArchives(distance)
		return self._getBestMatchingArchive(ret)

	# "Best" match is kind of a fuzzy term here. I define it as the archive with the
	# most files in common with the current archive.
	# If there are multiple archives with identical numbers of items in common,
	# the "best" is then the largest of those files
	# (I assume that the largest is probably either a 1. volume archive, or
	# 2. higher quality)
	def _getBestMatchingArchive(self, ret):
		# Short circuit for no matches
		if not len(ret):
			return None

		tmp = {}
		for key in ret.keys():
			tmp.setdefault(len(ret[key]), []).append(key)

		maxKey = max(tmp.keys())

		# If there is only one file with the most items, return that.
		if len(tmp[maxKey]) == 1:
			return tmp[maxKey].pop()

		items = [(os.path.getsize(item), item) for item in tmp[maxKey]]
		items.sort()

		# Finally, sort by size, return the biggest one of them
		return items.pop()[-1]


	def getMatchingArchives(self):
		self.log.info("Checking if %s contains any unique files.", self.archPath)

		matches = {}
		for fileN, fileCtnt in self.arch:
			fileCtnt = fileCtnt.read()
			dups = []

			fType = magic.from_buffer(fileCtnt)
			if fileN.endswith("Thumbs.db") and fType == b'Composite Document File V2 Document, No summary info':
				dups.append("Windows thumbnail file. Ignoring")
				self.log.info("Windows thumbnail database. Ignoring")
				continue

			hexHash = self.remote.root.getMd5Hash(fileCtnt)

			dupsIn = self.db.getOtherHashes(hexHash, fsMaskPath=self.archPath)
			for fsPath, internalPath, dummy_itemhash in dupsIn:

				isNotMasked =  any([fsPath.startswith(maskedPath) for maskedPath in self.maskedPaths])
				if os.path.exists(fsPath) and isNotMasked:
					matches.setdefault(fsPath, set()).add(fileN)
					dups.append((fsPath, internalPath, dummy_itemhash))
				elif not isNotMasked:
					# self.log.info("Match masked by filter: '%s'", fsPath)
					pass
				else:
					self.log.warn("Item '%s' no longer exists!", fsPath)
					self.db.deleteBasePath(fsPath)



			# Short circuit on unique item, since we are only checking if ANY item is unique
			if not dups:
				self.log.info("It contains at least one unique files.")
				# self.log.info("Unique file: '%s'", fileN)
				return {}

		self.log.info("It does not contain any unique files.")

		return matches


	# This really, /really/ feels like it should be several smaller functions, but I cannot see any nice ways to break it up.
	# It's basically like 3 loops rolled together to reduce processing time and lookups, and there isn't much I can do about that.
	def getPhashMatchingArchives(self, searchDistance=PHASH_DISTANCE_THRESHOLD):

		# self.db.deleteBasePath(self.archPath)

		self.log.info("Scanning for phash duplicates.")
		matches = {}

		for fileN, fileCtnt in self.arch:
			fileCtnt = fileCtnt.read()
			fName, hexHash, pHash, dummy_dHash, dummy_imX, dummy_imY = self.remote.root.hashFile(self.archPath, fileN, fileCtnt)

			if pHash == 0:
				self.log.warning("Skipping any checks for hash value of '%s', as it's uselessly common.", pHash)
				continue

			if pHash == None:
				dups = []
				fType = magic.from_buffer(fileCtnt)
				if fName.endswith("Thumbs.db") and fType == b'Composite Document File V2 Document, No summary info':
					dups.append("Windows thumbnail file. Ignoring")
					self.log.info("Windows thumbnail database. Ignoring")
					continue

				if fName.endswith("deleted.txt") and fType == b'ASCII text':
					dups.append("Removed advert note. Ignoring")
					self.log.info("Found removed advert note. Ignoring")
					continue

				self.log.warn("No phash for file '%s'! Wat?", (fName))
				self.log.warn("Returned pHash: '%s'", (pHash))
				self.log.warn("File size: %s", (len(fileCtnt)))
				self.log.warn("Guessed file type: '%s'", (fType))
				self.log.warn("Using binary dup checking for file!")

				dupsIn = self.db.getOtherHashes(hexHash, fsMaskPath=self.archPath)
				for fsPath, internalPath, dummy_itemhash in dupsIn:

					isNotMasked =  any([fsPath.startswith(maskedPath) for maskedPath in self.maskedPaths])
					if os.path.exists(fsPath) and isNotMasked:
						matches.setdefault(fsPath, set()).add(fileN)
						dups.append((fsPath, internalPath, dummy_itemhash))
					elif not isNotMasked:
						pass
						# self.log.info("Match masked by filter: '%s'", fsPath)
					else:
						self.log.warn("Item '%s' no longer exists!", fsPath)
						self.db.deleteBasePath(fsPath)

				self.log.warn("Found binary duplicates = %s", len(dups))

			else:

				proximateFiles = self.db.getWithinDistance(pHash, searchDistance)
				# self.log.info("File: '%s', '%s'. Number of matches %s", self.archPath, fileN, len(proximateFiles))

				dups = []

				for row in [match for match in proximateFiles if (match and match[1] != self.archPath)]:
					fsPath, internalPath = row[1], row[2]
					# print("'%s' '%s'" % (fsPath, internalPath))
					isNotMasked =  any([fsPath.startswith(maskedPath) for maskedPath in self.maskedPaths])


					if isNotMasked and os.path.exists(fsPath) :
						matches.setdefault(fsPath, set()).add(fileN)
						dups.append((internalPath, hexHash))
					elif not isNotMasked:
						# self.log.info("Match masked by filter: '%s'", fsPath)
						pass
					else:
						self.log.warn("Item '%s' no longer exists!", fsPath)
						self.db.deleteBasePath(fsPath)

			# Short circuit on unique item, since we are only checking if ANY item is unique

			if not dups:
				self.log.info("Archive contains at least one unique phash(es).")
				self.log.info("First unique file: '%s'", fileN)
				return {}

		self.log.info("Archive does not contain any unique phashes.")
		return matches


	def getHashes(self, shouldPhash=True):

		self.log.info("Getting item hashes for %s.", self.archPath)
		ret = []
		for fileN, fileCtnt in self.arch:
			ret.append(self.remote.root.hashFile(self.archPath, fileN, fileCtnt.read(), shouldPhash=shouldPhash))


		self.log.info("%s Fully hashed.", self.archPath)
		return ret

	def deleteArch(self, moveToPath=False):

		self.db.deleteBasePath(self.archPath)
		if not moveToPath:
			self.log.warning("Deleting archive '%s'", self.archPath)
			os.remove(self.archPath)
		else:
			dst = self.archPath.replace("/", ";")
			dst = os.path.join(moveToPath, dst)
			self.log.info("Moving item from '%s'", self.archPath)
			self.log.info("              to '%s'", dst)
			try:
				shutil.move(self.archPath, dst)
			except KeyboardInterrupt:
				raise
			except OSError:
				self.log.error("ERROR - Could not move file!")
				self.log.error(traceback.format_exc())


	def addNewArch(self, shouldPhash=True):

		self.log.info("Hashing file %s", self.archPath)

		# Delete any existing hashes that collide
		self.db.deleteBasePath(self.archPath)

		# And tell the remote hasher to process the new archive.
		self.hash.processArchive(self.archPath)


	# Proxy through to the archChecker from UniversalArchiveInterface
	@staticmethod
	def isArchive(archPath):
		return UniversalArchiveInterface.ArchiveReader.isArchive(archPath)


if __name__ == "__main__":
	import logSetup
	logSetup.initLogging()
	ac = ArchChecker('/media/Storage/Manga/13-nin no Short Suspense & Horror/13-nin no Short Suspense & Horror - c7.zip')
	ac.addNewArch()

