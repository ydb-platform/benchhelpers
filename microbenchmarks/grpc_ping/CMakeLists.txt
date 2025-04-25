cmake_minimum_required(VERSION 3.13)
project(grpc_ping CXX)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -O3 -DNDEBUG")
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -g")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Protobuf REQUIRED)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GRPC REQUIRED grpc++ grpc)

# Find the gRPC plugin
find_program(GRPC_CPP_PLUGIN grpc_cpp_plugin
    HINTS /usr/bin
    DOC "The gRPC C++ plugin"
)

if(NOT GRPC_CPP_PLUGIN)
    message(FATAL_ERROR "gRPC C++ plugin not found")
endif()

set(PROTOS_PATH ${CMAKE_CURRENT_SOURCE_DIR}/proto)
set(PROTO_FILES ${PROTOS_PATH}/debug.proto)

# Create build directory for generated proto files
set(PROTO_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/proto)
file(MAKE_DIRECTORY ${PROTO_GEN_DIR})

add_custom_command(
    OUTPUT
        ${PROTO_GEN_DIR}/debug.pb.cc
        ${PROTO_GEN_DIR}/debug.pb.h
        ${PROTO_GEN_DIR}/debug.grpc.pb.cc
        ${PROTO_GEN_DIR}/debug.grpc.pb.h
    COMMAND protoc
    ARGS
        --grpc_out=${PROTO_GEN_DIR}
        --cpp_out=${PROTO_GEN_DIR}
        -I ${PROTOS_PATH}
        --plugin=protoc-gen-grpc=${GRPC_CPP_PLUGIN}
        ${PROTO_FILES}
    DEPENDS ${PROTO_FILES}
    COMMENT "Generating gRPC and Protobuf files"
)

add_custom_target(proto_gen
    DEPENDS
        ${PROTO_GEN_DIR}/debug.pb.cc
        ${PROTO_GEN_DIR}/debug.pb.h
        ${PROTO_GEN_DIR}/debug.grpc.pb.cc
        ${PROTO_GEN_DIR}/debug.grpc.pb.h
)

add_executable(grpc_ping_client
    grpc_ping_client.cpp
    ${PROTO_GEN_DIR}/debug.pb.cc
    ${PROTO_GEN_DIR}/debug.grpc.pb.cc
)

add_dependencies(grpc_ping_client proto_gen)

target_include_directories(grpc_ping_client PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${Protobuf_INCLUDE_DIRS}
    ${GRPC_INCLUDE_DIRS}
    ${PROTO_GEN_DIR}
)

target_link_libraries(grpc_ping_client PRIVATE
    protobuf::libprotobuf
    ${GRPC_LIBRARIES}
    pthread
    dl
)