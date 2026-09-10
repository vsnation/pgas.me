#include "Shaders/common.h"
#include "Shaders/app_common_impl.h"
#include "Shaders/Ethash.h"
#include "pipe_contract.h"

namespace Pipe
{
#include "pipe_contract_sid.i"
}

namespace
{
    const char* TOKEN_CID = "tokenCID";
    const char* TOKEN_AID = "tokenAID";
    const char* CONTRACT_ID = "cid";
    const char* AMOUNT = "amount";
    const char* RELAYER_FEE = "relayerFee";
    const char* RECEIVER = "receiver";
    const char* MSG_ID = "msgId";
    const char* START_FROM = "startFrom";
    const char* RELAYER = "relayer";
    const char* INDEX = "index";
    const char* MAX_INDEX = "maxIndex";
    const char* INDEXES = "indexes";

    namespace Actions
    {
        const char* CREATE = "create";
        const char* VIEW = "view";
        const char* VIEW_PARAMS = "view_params";
        const char* SET_RELAYER = "set_relayer";
        const char* GET_PK = "get_pk";
        const char* SEND = "send";
        const char* RECEIVE = "receive";
        const char* PUSH_REMOTE = "push_remote";
        const char* VIEW_INCOMING = "view_incoming";
        const char* LOCAL_MSG_COUNT = "local_msg_count";
        const char* LOCAL_MSG = "local_msg";
        const char* REMOTE_MSG = "remote_msg";
        const char* MSG_STATUS = "msg_status";
    } // namespace Actions

    void OnError(const char* sz)
    {
        Env::DocAddText("error", sz);
    }

    // ── Receiver keys ──────────────────────────────────────────────────────────────────
    // The key a bridge message names is derived by THIS wallet from a blob (Env::DerivePk).
    // Upstream the blob is the contract id alone, so a wallet owns exactly one receiver key
    // per pipe and every message ever sent to it names the same 33 bytes. This build adds an
    // index: KeyID{cid, index} is a different blob, hence a different key, signed for by the
    // same wallet through the same SigRequest mechanism the contract already authorises
    // (Method_4 does Env::AddSig(msg.m_UserPK) — whichever key the message names).
    // Index 0 — or no index at all — is the LEGACY blob, byte-for-byte what upstream derives,
    // so every message already delivered to this wallet stays claimable and every call that
    // does not mention an index behaves exactly as before.
#pragma pack (push, 1)
    struct UserKeyID
    {
        ContractID m_Cid;   // first, so the legacy blob is this struct's first 32 bytes
        uint64_t m_Index;
    };
#pragma pack (pop)
    static_assert(sizeof(UserKeyID) == sizeof(ContractID) + sizeof(uint64_t), "UserKeyID must be packed");

    struct UserKey
    {
        // Owns the blob a SigRequest points into: it must outlive Env::GenerateKernel.
        UserKeyID m_Kid;

        void Init(const ContractID& cid, uint64_t idx)
        {
            _POD_(m_Kid.m_Cid) = cid;
            m_Kid.m_Index = idx;
        }

        const void* get_Ptr() const { return &m_Kid; }
        uint32_t get_Size() const { return m_Kid.m_Index ? sizeof(m_Kid) : sizeof(m_Kid.m_Cid); }

        void DerivePk(PubKey& pk) const { Env::DerivePk(pk, get_Ptr(), get_Size()); }
        void get_Sig(SigRequest& sig) const { sig.m_pID = get_Ptr(); sig.m_nID = get_Size(); }
    };

    // The receiver keys a view matches messages against: the legacy key (index 0) plus a
    // bounded window and/or an explicit list of indexed keys. Bounded, so one call can never
    // be asked to derive an unbounded number of keys.
    struct UserKeySet
    {
        static const uint32_t s_MaxIndexed = 256;
        static const uint32_t s_MaxKeys = s_MaxIndexed + 1;

        uint32_t m_Count = 0;
        uint64_t m_pIndex[s_MaxKeys];
        PubKey m_pPk[s_MaxKeys];

        bool Add(const ContractID& cid, uint64_t idx)
        {
            for (uint32_t i = 0; i < m_Count; i++)
                if (m_pIndex[i] == idx)
                    return true; // already in the set

            if (m_Count >= s_MaxKeys)
                return false;

            UserKey uk;
            uk.Init(cid, idx);
            uk.DerivePk(m_pPk[m_Count]);
            m_pIndex[m_Count] = idx;
            m_Count++;
            return true;
        }

        bool Find(const PubKey& pk, uint64_t& idx) const
        {
            for (uint32_t i = 0; i < m_Count; i++)
            {
                if (_POD_(m_pPk[i]) == pk)
                {
                    idx = m_pIndex[i];
                    return true;
                }
            }
            return false;
        }
    };

    struct ParamsPlus : public Pipe::Params
    {
        bool get(const ContractID& cid)
        {
            Env::Key_T<uint8_t> gk;
            _POD_(gk.m_Prefix.m_Cid) = cid;
            gk.m_KeyInContract = Pipe::PARAMS_KEY;

            if (Env::VarReader::Read_T(gk, *this))
                return true;

            OnError("no params");
            return false;
        }
    };

    struct IncomingWalker
    {
        const ContractID& m_Cid;
        IncomingWalker(const ContractID& cid) :m_Cid(cid) {}

        PubKey m_Relayer;
        Env::VarReaderEx<true> m_Reader;
        Env::Key_T<Pipe::RemoteMsgHdr::Key> m_Key;
        Pipe::RemoteMsgHdr m_Msg;


        bool Restart(uint64_t iStartFrom)
        {
            ParamsPlus params;
            if (!params.get(m_Cid))
                return false;

            m_Relayer = params.m_Relayer;

            Env::Key_T<Pipe::RemoteMsgHdr::Key> k1;
            k1.m_Prefix.m_Cid = m_Cid;
            k1.m_KeyInContract.m_MsgId_BE = Utils::FromBE(iStartFrom);

            auto k2 = k1;
            k2.m_KeyInContract.m_MsgId_BE = -1;

            m_Reader.Enum_T(k1, k2);
            return true;
        }

        bool MoveNext(const UserKeySet* pKeys, uint64_t& idx)
        {
            while (true)
            {
                if (!m_Reader.MoveNext_T(m_Key, m_Msg))
                    return false;

                if (pKeys && !pKeys->Find(m_Msg.m_UserPK, idx))
                    continue;

                return true;
            }
        }
    };

    void ViewIncoming(const ContractID& cid, const UserKeySet* pKeys, uint64_t iStartFrom)
    {
        Env::DocArray gr("incoming");

        IncomingWalker wlk(cid);
        if (!wlk.Restart(iStartFrom))
            return;

        uint64_t idx = 0;
        while (wlk.MoveNext(pKeys, idx))
        {
            Env::DocGroup gr("");
            Env::DocAddNum("MsgId", Utils::FromBE(wlk.m_Key.m_KeyInContract.m_MsgId_BE));
            Env::DocAddNum("amount", wlk.m_Msg.m_Amount);

            if (pKeys)
                Env::DocAddNum("index", idx); // which of our keys this message names; 0 = legacy
            else
                Env::DocAddBlob_T("User", wlk.m_Msg.m_UserPK);
        }
    }
} // namespace

namespace manager
{
    void Create()
    {
        Pipe::Create args;
        Env::DocGet(TOKEN_CID, args.m_TokenCID);
        Env::DocGetNum32(TOKEN_AID, &args.m_AssetID);

        Env::GenerateKernel(nullptr, args.s_iMethod, &args, sizeof(args), nullptr, 0, nullptr, 0, "create Pipe contract", 0);
    }

    void View()
    {
        EnumAndDumpContracts(Pipe::s_SID);
    }

    void ViewParams()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        Env::Key_T<uint8_t> key;
        key.m_KeyInContract = Pipe::PARAMS_KEY;
        key.m_Prefix.m_Cid = cid;

        Pipe::Params params;

        Env::VarReader::Read_T(key, params);

        Env::DocAddBlob_T("relayer pubkey", params.m_Relayer);
        Env::DocAddBlob_T("tocken CID", params.m_TokenCID);
        Env::DocAddNum64("token asset ID", params.m_AssetID);
    }

    void SetRelayer()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        Pipe::SetRelayer args;
        Env::DocGet(RELAYER, args.m_Relayer);

        Env::GenerateKernel(&cid, args.s_iMethod, &args, sizeof(args), nullptr, 0, nullptr, 0, "Set relayer public key", 0);
    }

    void GetPk()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        uint64_t idx = 0;
        Env::DocGet(INDEX, idx); // absent → 0 → the legacy blob

        UserKey uk;
        uk.Init(cid, idx);

        PubKey pk;
        uk.DerivePk(pk);
        Env::DocAddBlob_T("pk", pk);
        Env::DocAddNum("index", idx);
    }

    void SendFunds()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        ParamsPlus params;
        if (!params.get(cid))
            return;
        
        Pipe::SendFunds args;
        Env::DocGetNum64(AMOUNT, &args.m_Amount);
        Env::DocGetNum64(RELAYER_FEE, &args.m_RelayerFee);
        Env::DocGetBlobEx(RECEIVER, &args.m_Receiver, sizeof(args.m_Receiver));

        FundsChange fc;
        fc.m_Aid = params.m_AssetID;
        fc.m_Amount = args.m_Amount + args.m_RelayerFee;
        fc.m_Consume = 1;

        Env::GenerateKernel(&cid, args.s_iMethod, &args, sizeof(args), &fc, 1, nullptr, 0, "Send funds", 0);
    }

    void ReceiveFunds()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        ParamsPlus params;
        if (!params.get(cid))
            return;

        Pipe::ReceiveFunds args;
        Env::DocGetNum64(MSG_ID, &args.m_MsgId);

        Env::Key_T<Pipe::RemoteMsgHdr::Key> msgKey;
        Pipe::RemoteMsgHdr msg;

        msgKey.m_Prefix.m_Cid = cid;
        msgKey.m_KeyInContract.m_MsgId_BE = Utils::FromBE(args.m_MsgId);
        
        if (!Env::VarReader::Read_T(msgKey, msg))
        {
            OnError("msg with current id is absent");
            return;
        }

        // check msgId. maybe it is processed
        Env::Key_T<uint64_t> receivedKey;
        receivedKey.m_Prefix.m_Cid = cid;
        receivedKey.m_KeyInContract = args.m_MsgId;

        bool received = false;
        if (!Env::VarReader::Read_T(receivedKey, received))
        {
            OnError("msg with current id is absent");
            return;
        }

        if (received)
        {
            OnError("msg is processed");
            return;
        }

        uint64_t idx = 0;
        Env::DocGet(INDEX, idx); // absent → 0 → the legacy blob

        UserKey uk; // outlives GenerateKernel: the SigRequest points into it
        uk.Init(cid, idx);

        // Refuse early what the contract would refuse late: Method_4 needs a signature by the
        // key the message names, and the wallet can only sign for the blob it is handed. A
        // mismatch here would otherwise become a kernel the node rejects.
        PubKey pk;
        uk.DerivePk(pk);
        if (_POD_(pk) != msg.m_UserPK)
        {
            OnError("receiver key mismatch: this message was not sent to the key of this index");
            return;
        }

        FundsChange fc;
        fc.m_Aid = params.m_AssetID;
        fc.m_Amount = msg.m_Amount;
        fc.m_Consume = 0;

        SigRequest sig;
        uk.get_Sig(sig);

        Env::DocAddNum("index", idx);
        Env::GenerateKernel(&cid, args.s_iMethod, &args, sizeof(args), &fc, 1, &sig, 1, "Receive funds", 1200000);
    }

    void PushRemote()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        Pipe::PushRemote args;
        Env::DocGetNum64(MSG_ID, &args.m_MsgId);
        Env::DocGetNum64(AMOUNT, &args.m_RemoteMsg.m_Amount);
        Env::DocGetNum64(RELAYER_FEE, &args.m_RemoteMsg.m_RelayerFee);
        Env::DocGet(RECEIVER, args.m_RemoteMsg.m_UserPK);

        // check msgId. maybe it is processed
        Env::Key_T<uint64_t> receivedKey;
        receivedKey.m_Prefix.m_Cid = cid;
        receivedKey.m_KeyInContract = args.m_MsgId;

        bool received;
        if (Env::VarReader::Read_T(receivedKey, received))
        {
            OnError("msg is exist");
            return;
        }

        ParamsPlus params;
        if (!params.get(cid))
            return;

        FundsChange fc;
        fc.m_Aid = params.m_AssetID;
        fc.m_Amount = args.m_RemoteMsg.m_RelayerFee;
        fc.m_Consume = 0;

        SigRequest sig;
        sig.m_pID = &cid;
        sig.m_nID = sizeof(cid);

        Env::GenerateKernel(&cid, args.s_iMethod, &args, sizeof(args), &fc, 1, &sig, 1, "Push remote message", 0);
    }

    void ViewIncomingMsg()
    {
        ContractID cid;
        uint64_t startFrom = 0;
        Env::DocGet(CONTRACT_ID, cid);
        Env::DocGet(START_FROM, startFrom);

        UserKeySet keys;
        keys.Add(cid, 0); // the legacy key, always

        // Explicit list: indexes=<n;n;n>. The wallet splits the whole args string on ',', so
        // the list separator is anything that is not a digit — ';' by convention.
        char szList[2048];
        uint32_t nList = Env::DocGetText(INDEXES, szList, sizeof(szList));
        if (nList > sizeof(szList))
        {
            OnError("indexes list too long");
            return;
        }

        uint64_t maxIndex = 0;
        bool bHaveMax = Env::DocGet(MAX_INDEX, maxIndex);

        if (nList > 1)
        {
            uint64_t v = 0;
            bool bDigits = false;
            for (uint32_t i = 0; ; i++)
            {
                char c = szList[i];
                if ((c >= '0') && (c <= '9'))
                {
                    v = v * 10 + (c - '0');
                    bDigits = true;
                    continue;
                }
                if (bDigits && !keys.Add(cid, v))
                {
                    OnError("too many indexes (max 256)");
                    return;
                }
                v = 0;
                bDigits = false;
                if (!c)
                    break;
            }
        }
        else if (!bHaveMax)
            maxIndex = 64; // the default window when neither a list nor a bound is given

        if (maxIndex > UserKeySet::s_MaxIndexed)
        {
            OnError("maxIndex too large (max 256)");
            return;
        }
        for (uint64_t i = 1; i <= maxIndex; i++)
            keys.Add(cid, i);

        ViewIncoming(cid, &keys, startFrom);
    }

    void GetLocalMsgCount()
    {
        ContractID cid;
        Env::DocGet(CONTRACT_ID, cid);

        Env::Key_T<uint8_t> key;
        key.m_KeyInContract = Pipe::LOCAL_MSG_COUNTER_KEY;
        key.m_Prefix.m_Cid = cid;

        uint64_t localMsgCounter = 0;
        Env::VarReader::Read_T(key, localMsgCounter);

        Env::DocAddNum64("count", localMsgCounter);
    }

    void GetLocalMsg()
    {
        ContractID cid;
        uint64_t msgId;
        Env::DocGet(CONTRACT_ID, cid);
        Env::DocGetNum64(MSG_ID, &msgId);

        Env::Key_T<Pipe::LocalMsgHdr::Key> msgKey;
        msgKey.m_Prefix.m_Cid = cid;
        msgKey.m_KeyInContract.m_MsgId_BE = Utils::FromBE(msgId);

        Env::VarReader reader(msgKey, msgKey);

        uint32_t keySize = sizeof(msgKey);
        Pipe::LocalMsgHdr msg;
        uint32_t size = sizeof(msg);
        if (!reader.MoveNext(nullptr, keySize, &msg, size, 0))
        {
            OnError("msg with current id is absent");
            return;
        }

        Env::DocAddNum(AMOUNT, msg.m_Amount);
        Env::DocAddNum(RELAYER_FEE, msg.m_RelayerFee);
        Env::DocAddBlob_T(RECEIVER, msg.m_Receiver);
        Env::DocAddNum("height", msg.m_Height);
    }

    void GetRemoteMsg()
    {
        ContractID cid;
        uint64_t msgId;
        Env::DocGet(CONTRACT_ID, cid);
        Env::DocGetNum64(MSG_ID, &msgId);

        Env::Key_T<Pipe::RemoteMsgHdr::Key> msgKey;
        msgKey.m_Prefix.m_Cid = cid;
        msgKey.m_KeyInContract.m_MsgId_BE = Utils::FromBE(msgId);

        Env::VarReader reader(msgKey, msgKey);

        uint32_t keySize = sizeof(msgKey);
        Pipe::RemoteMsgHdr msg;
        uint32_t size = sizeof(msg);
        if (!reader.MoveNext(nullptr, keySize, &msg, size, 0))
        {
            OnError("msg with current id is absent");
            return;
        }

        Env::DocAddNum(AMOUNT, msg.m_Amount);
        Env::DocAddNum(RELAYER_FEE, msg.m_RelayerFee);
        Env::DocAddBlob_T(RECEIVER, msg.m_UserPK);
    }

    void GetMsgStatus()
    {
        ContractID cid;
        uint64_t msgId;
        Env::DocGet(CONTRACT_ID, cid);
        Env::DocGetNum64(MSG_ID, &msgId);

        Env::Key_T<uint64_t> key;
        key.m_Prefix.m_Cid = cid;
        key.m_KeyInContract = msgId;

        bool processed = false;

        if (Env::VarReader::Read_T(key, processed))
        {
            if (processed)
            {
                Env::DocAddNum64("status", 1);
            }
            else
            {
                Env::DocAddNum64("status", 2);
            }
            return;
        }

        Env::DocAddNum64("status", 0);
    }
} // namespace manager

BEAM_EXPORT void Method_0()
{
    // scheme
    Env::DocGroup root("");
    {
        Env::DocGroup grMethod(Actions::CREATE);
        Env::DocAddText(TOKEN_CID, "ContractID");
        Env::DocAddText(TOKEN_AID, "AssedID");
    }
    {
        Env::DocGroup grMethod(Actions::VIEW);
    }
    {
        Env::DocGroup grMethod(Actions::VIEW_PARAMS);
    }
    {
        Env::DocGroup grMethod(Actions::SET_RELAYER);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(RELAYER, "PubKey");
    }
    {
        Env::DocGroup grMethod(Actions::GET_PK);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(INDEX, "uint64 (optional; absent or 0 = the legacy key)");
    }
    {
        Env::DocGroup grMethod(Actions::SEND);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(AMOUNT, "uint64");
        Env::DocAddText(RELAYER_FEE, "uint64");
        Env::DocAddText(RECEIVER, "Address");
    }
    {
        Env::DocGroup grMethod(Actions::RECEIVE);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(MSG_ID, "uint64");
        Env::DocAddText(INDEX, "uint64 (optional; absent or 0 = the legacy key)");
    }
    {
        Env::DocGroup grMethod(Actions::PUSH_REMOTE);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(MSG_ID, "uint64");
        Env::DocAddText(AMOUNT, "uint64");
        Env::DocAddText(RELAYER_FEE, "uint64");
        Env::DocAddText(RECEIVER, "PubKey");
    }
    // local
    {
        Env::DocGroup grMethod(Actions::VIEW_INCOMING);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(START_FROM, "uint64 (optional; first MsgId to list)");
        Env::DocAddText(MAX_INDEX, "uint64 (optional; keys 1..maxIndex, at most 256; default 64 when no list)");
        Env::DocAddText(INDEXES, "text (optional; ';'-separated key indexes)");
    }
    {
        Env::DocGroup grMethod(Actions::LOCAL_MSG_COUNT);
        Env::DocAddText(CONTRACT_ID, "ContractID");
    }
    {
        Env::DocGroup grMethod(Actions::LOCAL_MSG);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(MSG_ID, "uint64");
    }
    {
        Env::DocGroup grMethod(Actions::REMOTE_MSG);
        Env::DocAddText(CONTRACT_ID, "ContractID");
        Env::DocAddText(MSG_ID, "uint64");
    }
}

BEAM_EXPORT void Method_1()
{
    Env::DocGroup root("");

    char szAction[20];

    if (!Env::DocGetText("action", szAction, sizeof(szAction)))
    {
        OnError("Action should be specified");
        return;
    }

    if (!Env::Strcmp(szAction, Actions::CREATE))
    {
        manager::Create();
    }
    else if (!Env::Strcmp(szAction, Actions::VIEW))
    {
        manager::View();
    }
    else if (!Env::Strcmp(szAction, Actions::VIEW_PARAMS))
    {
        manager::ViewParams();
    }
    else if (!Env::Strcmp(szAction, Actions::SET_RELAYER))
    {
        manager::SetRelayer();
    }
    else if (!Env::Strcmp(szAction, Actions::GET_PK))
    {
        manager::GetPk();
    }
    else if (!Env::Strcmp(szAction, Actions::SEND))
    {
        manager::SendFunds();
    }
    else if (!Env::Strcmp(szAction, Actions::RECEIVE))
    {
        manager::ReceiveFunds();
    }
    else if (!Env::Strcmp(szAction, Actions::PUSH_REMOTE))
    {
        manager::PushRemote();
    }
    else if (!Env::Strcmp(szAction, Actions::VIEW_INCOMING))
    {
        manager::ViewIncomingMsg();
    }
    else if (!Env::Strcmp(szAction, Actions::LOCAL_MSG_COUNT))
    {
        manager::GetLocalMsgCount();
    }
    else if (!Env::Strcmp(szAction, Actions::LOCAL_MSG))
    {
        manager::GetLocalMsg();
    }
    else if (!Env::Strcmp(szAction, Actions::REMOTE_MSG))
    {
        manager::GetRemoteMsg();
    }
    else if (!Env::Strcmp(szAction, Actions::MSG_STATUS))
    {
        manager::GetMsgStatus();
    }
    else
    {
        OnError("invalid Action.");
    }
}