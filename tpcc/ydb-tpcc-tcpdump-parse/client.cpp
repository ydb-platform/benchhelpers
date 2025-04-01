#include <algorithm>
#include <iomanip>
#include <iostream>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include <cstring>

#include <pcap.h>

#include "build/ydb.pb.h"
#include "ydb.pb.h"

constexpr size_t NewOrderSubQueriesCount = 11;

void displayHelp() {
    std::cout << "Usage: client [options] <file>\n"
              << "Options:\n"
              << "  -h, --help         Display this help message\n"
              << "  -n, --number <n>   Number of packets to parse\n"
              << "  --skip <n>         Number of first packets to skip\n"
              << std::endl;
}

std::string microsec_to_ms_str(u_int64_t microsec, bool skip_ms = false) {
    // convert to ms with 1 decimal place
    std::string result = std::to_string(microsec / 1000) + "." + std::to_string((microsec % 1000) / 100);
    if (!skip_ms) {
        result += " ms";
    }

    return result;
}

// I don't want to use extra dependencies for a such small tool, so here is a simple logger

enum EDEBUG_LEVEL {
    DEBUG_LEVEL_NONE = 0,
    DEBUG_LEVEL_DEBUG = 4,
    DEBUG_LEVEL_TRACE = 5
};

EDEBUG_LEVEL g_debug_level = DEBUG_LEVEL_NONE;

#define DEBUG(expr)                                        \
    do {                                                   \
        if (g_debug_level >= DEBUG_LEVEL_DEBUG) {          \
            std::cout << "[DEBUG]" << expr << std::endl;   \
        }                                                  \
    } while (0)

#define TRACE(expr)                                        \
    do {                                                   \
        if (g_debug_level >= DEBUG_LEVEL_TRACE) {          \
            std::cout << "[TRACE]" << expr << std::endl;   \
        }                                                  \
    } while (0)

//

struct IPAdress {
    IPAdress() {
        clear();
    }

    void clear() {
        memset(Bytes, 0, sizeof(Bytes));
    }

    bool empty() const {
        return Bytes[0] == 0;
    }

    bool operator==(const IPAdress& other) const {
        return memcmp(Bytes, other.Bytes, sizeof(Bytes)) == 0;
    }

    size_t hash() const {
        return Bytes[0] | (Bytes[1] << 8) | (Bytes[2] << 16) | (Bytes[3] << 24);
    }

    bool isIPv6() const {
        return Bytes[1] != 0 || Bytes[2] != 0 || Bytes[3] != 0;
    }

    void print(std::ostream& os) const {
        if (isIPv6()) {
            // not canonical, but close and readable
            os << std::hex << std::setw(2) << std::setfill('0') << (unsigned int)Bytes[0];
            os << std::hex << std::setw(2) << std::setfill('0') << (unsigned int)Bytes[1];
            for (int i = 2; i < 15; ++i) {
                if ((unsigned int)Bytes[i] == 0 && (unsigned int)Bytes[i+1] == 0) {
                    os << ":0";
                } else {
                    os << ":" << std::hex << std::setw(2) << std::setfill('0') << (unsigned int)Bytes[i];
                    os << std::hex << std::setw(2) << std::setfill('0') << (unsigned int)Bytes[i+1] << std::dec;
                }
            }
        } else {
            os << (unsigned int)Bytes[0] << "." << (unsigned int)Bytes[1]
               << "." << (unsigned int)Bytes[2] << "." << (unsigned int)Bytes[3];
        }
    }

public:
    // in case of IPv4 only first one is set
    u_char Bytes[16];
};

struct TCPEndpoint {
    void clear() {
        IP.clear();
        Port = 0;
    }

    bool empty() const {
        return IP.empty() && Port == 0;
    }

    bool operator==(const TCPEndpoint& other) const {
        return IP == other.IP && Port == other.Port;
    }

    size_t hash() const {
        return IP.hash() ^ Port;
    }

    void print() const {
        IP.print(std::cout);
        std::cout << " port:" << Port;
    }

public:
    IPAdress IP;
    u_int16_t Port = 0;
};

std::ostream& operator<<(std::ostream& os, const TCPEndpoint& endpoint) {
    endpoint.print();
    return os;
}

struct Http2StreamId {
    void clear() {
        Source.clear();
        StreamId = 0;
    }

    bool empty() const {
        return Source.empty() && StreamId == 0;
    }

    bool operator==(const Http2StreamId& other) const {
        return Source == other.Source && StreamId == other.StreamId;
    }

    bool operator!=(const Http2StreamId& other) const {
        return !(*this == other);
    }

    size_t hash() const {
        return Source.hash() ^ StreamId;
    }

    void print() const {
        Source.print();
        std::cout << " stream " << StreamId;
    }

public:
    TCPEndpoint Source;
    u_int32_t StreamId = 0;
};

std::ostream& operator<<(std::ostream& os, const Http2StreamId& streamId) {
    streamId.print();
    return os;
}

// it's filled while traversing through OSI levels
struct FrameInfo {
    FrameInfo(const timeval& t, long frame_number)
        : FrameNumber(frame_number)
        , TsUs(t.tv_sec * 1000000 + t.tv_usec)
    {}

public:
    long FrameNumber = 0;
    u_int64_t TsUs = 0;

    TCPEndpoint Source;
    TCPEndpoint Destination;
    long StreamId = 0;
};

// The whole transaction is executed within single YDB session and no other requests are allowed
// in the session while transaction is been executed. The session is available right from the firs
// execute data query request.
//
// Each request contains session ID, while server responses don't. Instead server replies have to
// be mapped with requests based on HTTP/2 stream id.
//
// Also, note tha the first request (execute data query) opens transaction and doesn't
// have transaction id, which is available in the first response or in subsequent
// requests (we consider multiple requests in one transaction).
class TrasnactionState {
public:
    TrasnactionState(const Http2StreamId& streamId, const std::string& session_id, u_int64_t ts)
        : SessionId(session_id)
        , StartTs(ts)
    {
        RequestLatencies.reserve(NewOrderSubQueriesCount); // our NewOrder has 10 queries + Commit

        // note, that transaction is started by regular execute data query request
        DEBUG("Transaction started in session " << SessionId << " with streamId " << streamId);
        start_request(streamId, session_id, ts);
    }

    void set_transaction_id(const std::string& transaction_id) {
        if (!TransactionId.empty() && TransactionId != transaction_id) {
            std::cerr << "Transaction id is already set to " << TransactionId
                      << ", new id: " << transaction_id << std::endl;
            throw std::runtime_error("Transaction id already set");
        }
        TransactionId = transaction_id;
    }

    const std::string& get_transaction_id() const {
        return TransactionId;
    }

    const std::string& get_session_id() const {
        return SessionId;
    }

    bool is_request_in_progress() const {
        return !CurrentRequestStreamId.empty();
    }

    Http2StreamId get_current_stream_id() const {
        return CurrentRequestStreamId;
    }

    void start_request(const Http2StreamId& streamId, const std::string& session_id, u_int64_t ts) {
        if (SessionId != session_id) {
            std::cerr << "Session id mismatch: " << SessionId << " vs. " << session_id << std::endl;
            throw std::runtime_error("Session id mismatch");
        }

        if (!CurrentRequestStreamId.empty()) {
            std::cerr << "Request already exists for stream " << CurrentRequestStreamId
                      << ", can't start request for stream " << streamId << std::endl;
            throw std::runtime_error("Request already exists");
        }

        CurrentRequestStreamId = streamId;
        CurrentRequestStartTs = ts;

        DEBUG("Started request in session " << SessionId
            << " with streamId " << streamId << " transaction " << TransactionId);
    }

    void finish_request(const Http2StreamId& streamId, u_int64_t ts) {
        // some sanity checks
        if (CurrentRequestStreamId.empty() || CurrentRequestStreamId != streamId) {
            std::cerr << "Finishing request for stream " << streamId << " while current is "
                      << CurrentRequestStreamId << std::endl;
            throw std::runtime_error("No request to finish");
        }

        if (CurrentRequestStartTs == 0) {
            throw std::runtime_error("finishing request without starting");
        }

        u_int64_t delta = 0;
        if (ts >= CurrentRequestStartTs) {
            delta = ts - CurrentRequestStartTs;
        }
        RequestLatencies.push_back(delta);

        CurrentRequestStreamId.clear();
        CurrentRequestStartTs = 0;

        DEBUG("Finished request in session " << SessionId << " with streamId " << CurrentRequestStreamId
            << " transaction " << TransactionId << " in " << microsec_to_ms_str(delta));
    }

    void start_commit(const Http2StreamId& streamId, const std::string& session_id, u_int64_t ts) {
        DEBUG("Started commit in session " << SessionId << " transaction " << TransactionId);

        // commit is the same request-response pair matched by streamId
        start_request(streamId, session_id, ts);
        IsCommitting = true;
    }

    bool is_committing() const {
        return IsCommitting;
    }

    void finish_transaction(const Http2StreamId& streamId, u_int64_t ts) {
        if (StartTs == 0) {
            std::cerr << "Transaction finished without openning" << std::endl;
            throw std::runtime_error("Transaction finished without openning");
        }

        finish_request(streamId, ts); // finish CommmitTransactionRequest

        EndTs = ts;

        for (const auto& latency: RequestLatencies) {
            ServerUs += latency;
        }

        DEBUG("Finished transaction in session " << SessionId << " with streamId " << streamId
            << " transaction " << TransactionId << " in " << microsec_to_ms_str(get_total_time_us()));
    }

    u_int64_t get_total_time_us() const {
        return EndTs - StartTs;
    }

    u_int64_t get_server_time() const {
        return ServerUs;
    }

    u_int64_t get_client_time() const {
        return get_total_time_us() - ServerUs;
    }

    const std::vector<u_int64_t>& get_request_latencies() const {
        return RequestLatencies;
    }

    void print(std::ostream& os) const {
        os << "Transaction " << TransactionId << " took " << microsec_to_ms_str(get_total_time_us())
           << " (client and net: " << microsec_to_ms_str(get_client_time())
        << ", server: " << microsec_to_ms_str(get_server_time())
           << "), with " << RequestLatencies.size() << " requests:";
        for (size_t i = 0; i < RequestLatencies.size(); ++i) {
            os << " r" << (i + 1) << ": " << microsec_to_ms_str(RequestLatencies[i], true);
        }
        os << std::endl;
    }

private:
    std::string SessionId;
    u_int64_t StartTs = 0;
    u_int64_t EndTs = 0;

    u_int64_t ServerUs = 0;

    Http2StreamId CurrentRequestStreamId;
    u_int64_t CurrentRequestStartTs = 0;

    bool IsCommitting = false;

    std::string TransactionId;

    // first – with transaction open, last – commit
    std::vector<u_int64_t> RequestLatencies;
};

using TrasnactionStatePtr = std::unique_ptr<TrasnactionState>;

std::ostream& operator<<(std::ostream& os, const TrasnactionState& state) {
    state.print(os);
    return os;
}

template<typename T>
struct Hasher {
    size_t operator()(const T& obj) const {
        return obj.hash();
    }
};

struct PacketParser {

    class TransactionHandler {
    public:
        TransactionHandler(const std::function<bool(const ydb::ExecuteDataQueryRequest&)>& filter)
            : Filter(filter)
        {}

        void handle_data_query_request(const ydb::ExecuteDataQueryRequest& request, const FrameInfo& frame_info) {
            Http2StreamId streamId;
            streamId.Source = frame_info.Source;
            streamId.StreamId = frame_info.StreamId;

            if (request.has_tx_control()) {
                const auto& tx_control = request.tx_control();
                switch (tx_control.tx_selector_case()) {
                case ydb::TransactionControl::kTxId: {
                    std::string tx_id;
                    if (request.has_tx_control()) {
                        const auto& tx_control = request.tx_control();
                        if (!tx_control.tx_id().empty()) {
                            tx_id = tx_control.tx_id();
                        }
                    }
                    handle_request(streamId, request.session_id(), tx_id, frame_info.TsUs);
                    break;
                }
                case ydb::TransactionControl::kBeginTx: {
                    if (!Filter || Filter(request)) {
                        start_transaction(streamId, request.session_id(), frame_info.TsUs);
                    } else {
                        ++RequestResponsesSkipped;
                    }
                    break;
                }
                default:
                    ++RequestResponsesSkipped;
                    return;
                }
            }
        }

        bool try_handle_data_query_response(const ydb::ExecuteDataQueryResponse& request, const FrameInfo& frame_info) {
            Http2StreamId streamId;
            streamId.Source = frame_info.Destination;
            streamId.StreamId = frame_info.StreamId;

            auto it = TransactionsByStream.find(streamId);
            if (it == TransactionsByStream.end()) {
                return false;
            }

            if (it->second->is_committing()) {
                return false;
            }

            handle_response(streamId, frame_info.TsUs);
            return true;
        }

        void handle_commit_request(const ydb::CommitTransactionRequest request, const FrameInfo& frame_info) {
            Http2StreamId streamId;
            streamId.Source = frame_info.Source;
            streamId.StreamId = frame_info.StreamId;

            handle_request(streamId, request.session_id(), request.tx_id(), frame_info.TsUs, true);
        }

        bool try_handle_commit_response(const ydb::CommitTransactionResponse, const FrameInfo& frame_info) {
            Http2StreamId streamId;
            streamId.Source = frame_info.Destination;
            streamId.StreamId = frame_info.StreamId;

            auto it = TransactionsByStream.find(streamId);
            if (it == TransactionsByStream.end()) {
                return false;
            }

            if (!it->second->is_committing()) {
                return false;
            }

            handle_commit_response(streamId, frame_info.TsUs);
            return true;
        }

        void calculate_results() {
            std::sort(FinishedTransactions.begin(), FinishedTransactions.end(),
                [](const TrasnactionStatePtr& a, const TrasnactionStatePtr& b) {
                    return (a->get_total_time_us()) < (b->get_total_time_us());
                });

            ClientLatencies.reserve(FinishedTransactions.size());
            for (const auto& transaction: FinishedTransactions) {
                ClientLatencies.push_back(transaction->get_client_time());
            }
            std::sort(ClientLatencies.begin(), ClientLatencies.end());

            ServerLatencies.reserve(FinishedTransactions.size());
            ServerQueryLatencies.reserve(FinishedTransactions.size() * NewOrderSubQueriesCount);
            for (const auto& transaction: FinishedTransactions) {
                ServerLatencies.push_back(transaction->get_server_time());
                for (auto latency: transaction->get_request_latencies()) {
                    ServerQueryLatencies.push_back(latency);
                }
            }
            std::sort(ServerLatencies.begin(), ServerLatencies.end());
            std::sort(ServerQueryLatencies.begin(), ServerQueryLatencies.end());
        }

        void print(std::ostream& os, long top_n) const {
            if (FinishedTransactions.empty()) {
                os << "No transactions finished";
                return;
            }

            os << "Processed " << RequestResponsesProcessed << " requests and responses, skipped "
               << RequestResponsesSkipped << std::endl
               << "Total transactions aborted: " << TransactionsAborted << std::endl
               << "Total transaction id mismatch: " << TransactionIdMismatch << std::endl
               << "Total request-response mismatch: " << RequestResponseMismatch << std::endl
               << "Total transactions committed: " << FinishedTransactions.size() << std::endl;

            const auto& percentiles = {0.5, 0.9, 0.95, 0.99};

            os << "Total time percentiles: " << std::endl;
            for (double percentile: percentiles) {
                size_t index = (size_t)(FinishedTransactions.size() * percentile);
                os << (int)(percentile * 100) << "%: "
                   << microsec_to_ms_str(FinishedTransactions[index]->get_total_time_us()) << std::endl;
            }

            os << "Client time percentiles: " << std::endl;
            for (double percentile: percentiles) {
                size_t index = (size_t)(ClientLatencies.size() * percentile);
                os << (int)(percentile * 100) << "%: "
                   << microsec_to_ms_str(ClientLatencies[index]) << std::endl;
            }

            os << "Server time percentiles: " << std::endl;
            for (double percentile: percentiles) {
                size_t index = (size_t)(ServerLatencies.size() * percentile);
                os << (int)(percentile * 100) << "%: "
                   << microsec_to_ms_str(ServerLatencies[index]) << std::endl;
            }

            os << "Server time query percentiles: " << std::endl;
            for (double percentile: percentiles) {
                size_t index = (size_t)(ServerQueryLatencies.size() * percentile);
                os << (int)(percentile * 100) << "%: "
                   << microsec_to_ms_str(ServerQueryLatencies[index]) << std::endl;
            }

            if (top_n == std::numeric_limits<long>::max()) {
                os << "Transactions by latency:\n";
            } else {
                os << "Top " << top_n << " transactions by latency:\n";
            }
            for (auto it = FinishedTransactions.rbegin();
                      it != FinishedTransactions.rend() && it - FinishedTransactions.rbegin() < top_n; ++it) {
                os << **it;
            }
        }
    private:
        void start_transaction(const Http2StreamId& streamId, const std::string& session_id, u_int64_t ts) {
            if (auto it = TransactionsByStream.find(streamId); it != TransactionsByStream.end()) {
                std::cerr << "Transaction already exists for stream " << streamId << std::endl;

                TransactionsByStream.erase(streamId);
                ActiveTransactions.erase(session_id);
                ++RequestResponseMismatch;
                return;
            }

            if (auto it = ActiveTransactions.find(session_id); it != ActiveTransactions.end()) {
                // since it is missing in TransactionsByStream, there is no active request.
                // some transactions are aborted, but we don't handle it in response processing,
                // so for simplicity we can skip aborted transaction and start new one.
                ActiveTransactions.erase(it);
                ++TransactionsAborted;
            }

            auto state_ptr = std::make_unique<TrasnactionState>(streamId, session_id, ts);
            TransactionsByStream[streamId] = state_ptr.get();
            ActiveTransactions.emplace(session_id, std::move(state_ptr));

            ++RequestResponsesProcessed;
        }

        void handle_request(
                const Http2StreamId& streamId,
                const std::string& session_id,
                const std::string& tx_id,
                u_int64_t ts,
                bool is_commit = false)
        {
            if (session_id.empty()) {
                std::cerr << "Empty session in request!\n";
                throw std::runtime_error("Empty session in request");
            }

            auto it = ActiveTransactions.find(session_id);
            if (it == ActiveTransactions.end()) {
                // e.g. our capture started after transaction have been started
                // also it might be a request from transaction we're not interested in
                ++RequestResponsesSkipped;
                return;
            }

            auto& state_ptr = it->second;

            if (const auto& current_stream_id = state_ptr->get_current_stream_id(); !current_stream_id.empty()) {
                std::cerr << "Can't start request " << streamId << " in session " << session_id
                    << ", because waiting for the response for " << current_stream_id << std::endl;

                TransactionsByStream.erase(current_stream_id);
                ActiveTransactions.erase(it);
                ++RequestResponseMismatch;
                return;
            }

            if (const auto& current_transaction_id = state_ptr->get_transaction_id(); current_transaction_id.empty()) {
                state_ptr->set_transaction_id(tx_id);
            } else if (current_transaction_id != tx_id) {
                Http2StreamId streamFound;
                for (const auto& [stream, transaction] : TransactionsByStream) {
                    if (transaction == state_ptr.get()) {
                        streamFound = stream;
                        break;
                    }
                }
                std::cerr << "Transaction id mismatch: " << current_transaction_id
                          << " vs. " << tx_id << " for stream " << streamFound
                          << std::endl;

                if (!streamFound.empty()) {
                    TransactionsByStream.erase(streamFound);
                }

                ActiveTransactions.erase(it);
                ++TransactionIdMismatch;
                return;
            }

            if (!is_commit) {
                state_ptr->start_request(streamId, session_id, ts);
            } else {
                state_ptr->start_commit(streamId, session_id, ts);
            }

            TransactionsByStream[streamId] = state_ptr.get();
            ++RequestResponsesProcessed;
        }

        void handle_response(const Http2StreamId& streamId, u_int64_t ts) {
            auto it = TransactionsByStream.find(streamId);
            if (it == TransactionsByStream.end()) {
                ++RequestResponsesSkipped;
            }

            it->second->finish_request(streamId, ts);
            ++RequestResponsesProcessed;

            TransactionsByStream.erase(it);
        }

        void handle_commit_response(const Http2StreamId& streamId, u_int64_t ts) {
            auto it = TransactionsByStream.find(streamId);
            if (it == TransactionsByStream.end()) {
                ++RequestResponsesSkipped;
                return;
            }

            it->second->finish_transaction(streamId, ts);

            auto it_session = ActiveTransactions.find(it->second->get_session_id());
            if (it_session == ActiveTransactions.end()) {
                std::cerr << "Transaction not found for session " << it->second->get_session_id() << std::endl;
                throw std::runtime_error("Transaction not found for session");
            }

            auto state_ptr = std::move(it_session->second);
            ActiveTransactions.erase(it_session);
            TransactionsByStream.erase(it);

            FinishedTransactions.emplace_back(std::move(state_ptr));
            ++RequestResponsesProcessed;
        }

    private:
        const std::function<bool(const ydb::ExecuteDataQueryRequest&)>& Filter;

        std::unordered_map<std::string, TrasnactionStatePtr> ActiveTransactions;
        std::unordered_map<Http2StreamId, TrasnactionState *, Hasher<Http2StreamId>> TransactionsByStream;

        std::vector<TrasnactionStatePtr> FinishedTransactions;
        std::vector<long> ClientLatencies;
        std::vector<long> ServerLatencies;
        std::vector<long> ServerQueryLatencies;

        size_t RequestResponsesProcessed = 0;
        size_t RequestResponsesSkipped = 0;
        size_t TransactionsAborted = 0;
        size_t TransactionIdMismatch = 0;
        size_t RequestResponseMismatch = 0;
    };

    PacketParser(
            const std::function<bool(const ydb::ExecuteDataQueryRequest&)>& filter,
            long skip_n)
        : transaction_handler(filter)
        , NumberingOffset(skip_n)
    {}

    void handle_ethernet_frame(const struct pcap_pkthdr *header, const u_char *frame) {
        const size_t ETHERNET_HEADER_SIZE = 14; // assume no P/Q tags present

        // assume there are no IPv4 options and no IPv6 extension headers
        const size_t IPV4_HEADER_SIZE = 20;
        const size_t IPV6_HEADER_SIZE = 40;

        const size_t TCP_HEADER_SIZE_NO_OPTIONS = 20;

        ++ParsedCount;
        const long currentPacketNumber = ParsedCount + NumberingOffset;

        if (header->len < ETHERNET_HEADER_SIZE) {
            return;
        }

        FrameInfo frame_info(header->ts, currentPacketNumber);

        // note, that frame doesn't include ethernet's preamble and SFD (start frame delimiter),
        // i.e. type is located at offset 12-13.
        size_t ip_header_size;
        if (frame[12] == 0x08 && frame[13] == 0x00) {
            ip_header_size = IPV4_HEADER_SIZE;
        } else if (frame[12] == 0x86 && frame[13] == 0xdd) {
            ip_header_size = IPV6_HEADER_SIZE;
        } else {
            std::cerr << "Packet " << currentPacketNumber << " is not an IPv4 or IPv6 packet!\n";
            throw std::runtime_error("Not an IP packet");
        }

        // sanity checks
        if (header->len < ETHERNET_HEADER_SIZE + ip_header_size) {
            std::cerr << "Packet " << currentPacketNumber << " doesn't seem to have proper IP header!\n";
            throw std::runtime_error("No IP header");
        }

        if (header->len < ETHERNET_HEADER_SIZE + ip_header_size + TCP_HEADER_SIZE_NO_OPTIONS) {
            std::cerr << "Packet " << currentPacketNumber << " doesn't seem to have TCP segment!\n";
            throw std::runtime_error("No TCP segment");
        }

        // IP

        const u_char *ip_header = frame + ETHERNET_HEADER_SIZE;
        if (ip_header_size == IPV4_HEADER_SIZE) {
            frame_info.Source.IP.Bytes[0] = ip_header[12];
            frame_info.Source.IP.Bytes[1] = ip_header[13];
            frame_info.Source.IP.Bytes[2] = ip_header[14];
            frame_info.Source.IP.Bytes[3] = ip_header[15];

            frame_info.Destination.IP.Bytes[0] = ip_header[16];
            frame_info.Destination.IP.Bytes[1] = ip_header[17];
            frame_info.Destination.IP.Bytes[2] = ip_header[18];
            frame_info.Destination.IP.Bytes[3] = ip_header[19];
        } else {
            for (size_t i = 0; i < 16; ++i) {
                frame_info.Source.IP.Bytes[i] = ip_header[8 + i];
            }

            for (size_t i = 0; i < 16; ++i) {
                frame_info.Destination.IP.Bytes[i] = ip_header[24 + i];
            }
        }

        // TCP

        const u_char *tcp_header = frame + ETHERNET_HEADER_SIZE + ip_header_size;
        frame_info.Source.Port = tcp_header[1] | (tcp_header[0] << 8);
        frame_info.Destination.Port = tcp_header[3] | (tcp_header[2] << 8);
        size_t tcp_header_length = (tcp_header[12] >> 4) * 4;

        const u_char *tcp_payload = tcp_header + tcp_header_length;
        size_t payload_length = header->len - (tcp_payload - frame);

        TRACE("Frame " << currentPacketNumber << " with IP header length: " << ip_header_size
            << ", TCP header length: " << tcp_header_length << " and TCP payload length: " << payload_length
            << ", from: " << frame_info.Source << " to " << frame_info.Destination);

        if (payload_length == 0) {
            return;
        }

        handle_http2(tcp_payload, payload_length, frame_info);
    }

    void handle_http2(const u_char *tcp_payload, size_t tcp_payload_length, FrameInfo& frame_info) {
        const size_t HTTP2_FRAME_HEADER_SIZE = 9;

        // sanity check: there should be at least 1 frame
        if (tcp_payload_length < HTTP2_FRAME_HEADER_SIZE) {
            return;
        }

        // there might be multiple frames, parse all
        const u_char* current_frame = tcp_payload;
        int frame_num = 1;

        while (current_frame + HTTP2_FRAME_HEADER_SIZE < tcp_payload + tcp_payload_length) {
            // process HTTP2 frame header
            uint32_t length = current_frame[2] | (current_frame[1] << 8) | (current_frame[0] << 16);
            uint8_t type = (int)current_frame[3];
            uint32_t streamId = current_frame[8] | (current_frame[7] << 8) | (current_frame[6] << 16)
                | ((current_frame[5] & ~(1 << 7)) << 24);

            // we don't bother to handle HTTP/2 headers because of HPACK: if we get dump in the middle of
            // the stream, we won't be able to decode them. That is why we parse GRPC payload directly.

            const u_char* frame_payload = current_frame + HTTP2_FRAME_HEADER_SIZE;
            const char* type_str = "other";
            switch (type) {
            case 0x01: {
                type_str = "headers";
                break;
            }
            case 0x00: {
                type_str = "data";
                frame_info.StreamId = streamId;
                handle_grpc(frame_payload, length, frame_info);
                break;
            }
            default:
                // just skip
                break;
            }

            TRACE("HTTP2 frame " << frame_num << " of type " << type_str << ", streamId: " << streamId
                << ", length " << length << std::endl);

            current_frame += HTTP2_FRAME_HEADER_SIZE + length;
            ++frame_num;
        }

        return;
    }

    void handle_grpc(const u_char *frame_payload, size_t frame_payload_length, const FrameInfo& frame_info) {
        const size_t GRPC_HEADER_SIZE = 5;

        // sanity check: there should be at least 5 bytes
        if (frame_payload_length < GRPC_HEADER_SIZE) {
            return;
        }

        uint32_t length = frame_payload[4] | (frame_payload[3] << 8) | (frame_payload[2] << 16)
            | (frame_payload[1] << 24);

        const u_char* grpc_payload = frame_payload + GRPC_HEADER_SIZE;
        size_t grpc_payload_length = frame_payload_length - GRPC_HEADER_SIZE;

        TRACE("gRPC payload length " << grpc_payload_length << std::endl);

        // Here, we make an assumption that if we have parsed the protobuf and it contains expected fields,
        // then we have guessed the message type.
        // Note, the check order makes sense: we go from messages we can identify here, to the messages
        // we can identify using YDB session state.

        // note, that it might contain the same as commitRequest + query, so should be the first
        ydb::ExecuteDataQueryRequest executeDataQueryRequest;
        if (executeDataQueryRequest.ParseFromArray(grpc_payload, grpc_payload_length)) {
            if (executeDataQueryRequest.has_query() && !executeDataQueryRequest.session_id().empty()) {
                TRACE("Parsed ExecuteDataQueryRequest:\n" << executeDataQueryRequest.Utf8DebugString());
                transaction_handler.handle_data_query_request(executeDataQueryRequest, frame_info);
                return;
            }
        }

        ydb::CommitTransactionRequest commitRequest;
        if (commitRequest.ParseFromArray(grpc_payload, grpc_payload_length)) {
            if (!commitRequest.session_id().empty() && !commitRequest.tx_id().empty()) {
                TRACE("Parsed commitRequest:\n" << commitRequest.Utf8DebugString());
                transaction_handler.handle_commit_request(commitRequest, frame_info);
                return;
            }
        }

        // CommitTransactionResponse and ExecuteDataQueryResponse seem to have identical fields (at least
        // without looking deeper), but we can handle them based on YDB session state

        ydb::ExecuteDataQueryResponse executeDataQueryResponse;
        if (executeDataQueryResponse.ParseFromArray(grpc_payload, grpc_payload_length)) {
            if (executeDataQueryResponse.has_operation()) {
                if (transaction_handler.try_handle_data_query_response(executeDataQueryResponse, frame_info)) {
                    TRACE("ExecuteDataQueryResponse:\n" << executeDataQueryResponse.Utf8DebugString());
                    return;
                }
            }
        }

        ydb::CommitTransactionResponse commitResponse;
        if (commitResponse.ParseFromArray(grpc_payload, grpc_payload_length)) {
            if (commitResponse.has_operation()) {
                if (transaction_handler.try_handle_commit_response(commitResponse, frame_info)) {
                    TRACE("CommitTransactionResponse:\n" << commitResponse.Utf8DebugString());
                    return;
                }
            }
        }
    }

    void process_print_results(long top_n) {
        transaction_handler.calculate_results();
        transaction_handler.print(std::cout, top_n);
    }

public:
    TransactionHandler transaction_handler;

    long NumberingOffset = 0;
    long ParsedCount = 0;
};

int main(int argc, char *argv[]) {
    std::string file_path;
    long n = std::numeric_limits<long>::max();
    long skip_n = 0;
    long top_n = 50;

    bool all_transaction_types = false;

    if (argc < 2) {
        std::cerr << "Too few arguments\n";
        displayHelp();
        return -1;
    }

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];

        if (arg == "-h" || arg == "--help") {
            displayHelp();
            return 0;
        } else if ((arg == "-n" || arg == "--number") && (i + 1 < argc)) {
            n = std::stol(argv[++i]);
        } else if (arg == "--skip" && (i + 1 < argc)) {
            skip_n = std::stol(argv[++i]);
        } else if (arg == "--print-all-transactions") {
            top_n = std::numeric_limits<long>::max();
        } else if (arg == "--all-types") {
            all_transaction_types = true;
        } else if (arg == "--debug") {
            g_debug_level = DEBUG_LEVEL_DEBUG;
        } else if (arg == "--trace") {
            g_debug_level = DEBUG_LEVEL_TRACE;
        } else if (arg.size() > 0 && arg[0] != '-') {
            if (file_path.size()) {
                std::cerr << "Duplicated free arg: " << arg << std::endl;
                displayHelp();
                return -1;
            }
            file_path = arg;
        } else {
            std::cerr << "Unknown option: " << arg << std::endl;
            displayHelp();
            return -1;
        }
    }

    std::function<bool(const ydb::ExecuteDataQueryRequest&)> filter;
    if (!all_transaction_types) {
        filter = [](const ydb::ExecuteDataQueryRequest& request) {
            static const std::string get_customer_query = "SELECT C_DISCOUNT, C_LAST, C_CREDIT";
            const auto& query = request.query();
            if (query.yql_text().find(get_customer_query) != std::string::npos) {
                return true;
            }

            return false;
        };
    }

    auto MyLogHandler = [] (google::protobuf::LogLevel level, const char* filename, int line, const std::string& message)
    {
        // We "guess" message type by parsing it and checking the fields + maintaining YDB session and HTTP/2 stream.
        // That is why protobuf errors to many errors and we don't want to print them.
    };
    google::protobuf::SetLogHandler(MyLogHandler);

    char errbuf[PCAP_ERRBUF_SIZE];
    pcap_t* handle = pcap_open_offline(file_path.c_str(), errbuf);
    if (handle == nullptr) {
        std::cerr << "Error opening " << file_path << ": " << errbuf << std::endl;
        return 1;
    }

    struct pcap_pkthdr* header;
    const u_char* packet;
    if (skip_n > 0) {
        for (int i = 0; i < skip_n; ++i) {
            // we don't check for errors intentionally
            pcap_next_ex(handle, &header, &packet);
        }
    }

    try {
        PacketParser parser(filter, skip_n);
        while (int result = pcap_next_ex(handle, &header, &packet) == 1 && parser.ParsedCount < n) {
            parser.handle_ethernet_frame(header, packet);
        }

        parser.process_print_results(top_n);
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }

    return 0;
}
