cmake_minimum_required(VERSION 3.10)

project(YdbTpccTcpdump)

set(CMAKE_CXX_STANDARD 17)

find_package(PkgConfig REQUIRED)
pkg_check_modules(PCAP REQUIRED libpcap)
include_directories(${PCAP_INCLUDE_DIRS})

find_package(Protobuf REQUIRED)
include_directories(${Protobuf_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_BINARY_DIR})
set(PROTO_FILES ydb.proto)
protobuf_generate_cpp(PROTO_SRCS PROTO_HDRS ${PROTO_FILES})


add_executable(ydb_tpcc_tcpdump_parse client.cpp ${PROTO_SRCS} ${PROTO_HDRS})

target_link_libraries(ydb_tpcc_tcpdump_parse ${PCAP_LIBRARIES} ${Protobuf_LIBRARIES})
