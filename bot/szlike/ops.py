"""GraphQL 业务操作集合。所有 query/mutation 都来自实测 schema。"""
from __future__ import annotations

import json
from typing import Any

from .client import Client

# ---------- Query：读 ----------

Q_VIEWER = """
query Q { viewer {
  id unionId role { role }
  user { id username profile { nickname grade institute } }
  viewerVerify { school { id name enShort } campus { id name } studentId }
} }
"""

Q_TOPICS = """
query Q($schoolId: ID!) { topics(input:{schoolId:$schoolId, first:100}) {
  edges { node { id name subtopics { edges { node { id name } } } } }
} }
"""

Q_POSTS = """
query Q($schoolId: ID, $orderBy: PostOrder, $after: String) {
  posts(filter:{schoolId:$schoolId}, orderBy:$orderBy, first:10, after:$after) {
    edges { node {
      id content images isAnonymous createdAt likeCount commentCount
      viewerHasLiked viewerCanDelete
      creator { id username profile { nickname } }
      topic { id name }
      subtopic { id name }
    } }
    totalCount
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_POST = """
query Q($id: ID!) { post(id:$id) {
  id content images isAnonymous createdAt likeCount commentCount viewerHasLiked viewerCanDelete
  creator { id username profile { nickname } }
  topic { id name }
} }
"""

Q_COMMENTS = """
query Q($postId: ID!, $order: PostCommentOrder!, $after: String) {
  comments(postId:$postId, first:10, order:$order, after:$after) {
    edges { node {
      id content isAnonymous createdAt likeCount subcommentCount viewerHasLiked viewerCanDelete
      creator { id username profile { nickname } }
      subcomments(first:2) { edges { node {
        id content createdAt
        creator { id username profile { nickname } }
        replyTo { id }
      } } }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_LATEST_REPLIES = """
query Q($afterComment: String, $afterSubcomment: String) {
  viewer { user {
    id
    userComments(type: RECEIVED, first: 5, after: $afterComment) {
      edges { node {
        id content isAnonymous createdAt
        creator { id username profile { nickname } }
        post { id content }
      } }
    }
    userSubcomments(type: RECEIVED, first: 5, after: $afterSubcomment) {
      edges { node {
        id content isAnonymous createdAt
        creator { id username profile { nickname } }
        comment { id content }
      } }
    }
  } }
}
"""

# 楼中楼回复必须看到「整条帖 + 原评论 + 楼里已经聊到哪一步」。
# 只给一句 comment.content 的话，模型会答非所问：人问「那家店还开吗」，
# 它回「确实挺好吃的」——因为看不到上一条是谁在问什么。
Q_COMMENT_CONTEXT = """
query Q($id: ID!) {
  comment(id:$id) {
    id content createdAt
    creator { id profile { nickname } }
    post { id content }
    subcomments(first:20) {
      edges { node {
        id content createdAt
        replyTo { id }
        creator { id profile { nickname } }
      } }
    }
  }
}
"""

Q_MY_POSTS = """
query Q($id: ID!, $after: String) { user(userId:$id) {
  userPosts(first:10, after:$after, type:CREATED) {
    edges { node {
      id content createdAt likeCount commentCount viewerCanDelete isAnonymous
      topic { id name } subtopic { id name }
    } }
    pageInfo { hasNextPage endCursor }
  }
} }
"""

Q_MY_COMMENTS = """
query Q($id: ID!, $after: String) { user(userId:$id) {
  userComments(first:15, after:$after, type:CREATED) {
    edges { node {
      id content createdAt isAnonymous likeCount subcommentCount
      viewerCanDelete viewerHasLiked
      post { id content }
    } }
    pageInfo { hasNextPage endCursor }
  }
} }
"""

Q_MY_SUBCOMMENTS = """
query Q($id: ID!, $after: String) { user(userId:$id) {
  userSubcomments(first:15, after:$after, type:CREATED) {
    edges { node {
      id content createdAt isAnonymous likeCount viewerCanDelete
      replyTo { id }
      comment { id content post { id content } }
    } }
    pageInfo { hasNextPage endCursor }
  }
} }
"""

# ---------- Mutation：写 ----------

M_CREATE_POST = """
mutation M($content: String!, $images: [URI!]!, $schoolId: ID!, $topicId: ID!,
           $subtopicId: ID, $isAnonymous: Boolean!, $activitiesInfo: [String!]) {
  post: createPost(input:{
    content:$content, images:$images, isAnonymous:$isAnonymous,
    topicId:$topicId, subtopicId:$subtopicId, schoolId:$schoolId,
    activitiesInfo:$activitiesInfo
  }) { id content createdAt }
}
"""

M_CREATE_COMMENT = """
mutation M($postId: ID!, $content: String!, $images: [URI!]!, $isAnonymous: Boolean!) {
  comment: createComment(input:{
    postId:$postId, content:$content, images:$images, isAnonymous:$isAnonymous
  }) { id content createdAt }
}
"""

M_CREATE_SUBCOMMENT = """
mutation M($commentId: ID!, $content: String!, $isAnonymous: Boolean!, $replyToId: ID) {
  subcomment: createSubcomment(input:{
    commentId:$commentId, content:$content, isAnonymous:$isAnonymous, replyToId:$replyToId
  }) { id content createdAt }
}
"""

M_ADD_LIKE = """
mutation M($id: ID!) { addLike(input:{id:$id}) { __typename } }
"""

M_REMOVE_LIKE = """
mutation M($id: ID!) { removeLike(input:{id:$id}) { __typename } }
"""

M_ADD_MARK = """
mutation M($postId: ID!) { addMark(input:{postId:$postId}) { id } }
"""

M_DELETE_POST = """
mutation M($id: ID!) { deletePost(input:{id:$id}) }
"""

M_DELETE_COMMENT = """
mutation M($id: ID!) { deleteComment(input:{id:$id}) }
"""

M_DELETE_SUBCOMMENT = """
mutation M($id: ID!) { deleteSubcomment(input:{id:$id}) }
"""

M_REGISTER = """
mutation M($u: String!, $p: String!) {
  register(input:{basicInput:{username:$u, password:$p}}) {
    id accessToken refreshToken
  }
}
"""


# ---------- 封装 ----------


def viewer(client: Client) -> dict:
    return client.gql(Q_VIEWER, operation="Q")["viewer"]


def topics(client: Client, school_id: str) -> list[dict]:
    edges = client.gql(Q_TOPICS, {"schoolId": school_id}, "Q")["topics"]["edges"]
    return [e["node"] for e in edges]


def list_posts(
    client: Client, school_id: str, order_by: str = "LATEST", after: str | None = None
) -> dict:
    return client.gql(Q_POSTS, {"schoolId": school_id, "orderBy": order_by, "after": after}, "Q")[
        "posts"
    ]


def get_post(client: Client, post_id: str) -> dict:
    return client.gql(Q_POST, {"id": post_id}, "Q")["post"]


def feed_posts(client: Client, school_id: str, pages: int = 1) -> list[dict]:
    """翻若干页最新帖，返回扁平节点列表（按时间倒序，已去重）。"""
    out: list[dict] = []
    seen: set[str] = set()
    after = None
    for _ in range(max(1, pages)):
        d = list_posts(client, school_id, order_by="LATEST", after=after)
        for e in d.get("edges") or []:
            n = e["node"]
            if n["id"] not in seen:
                seen.add(n["id"])
                out.append(n)
        info = d.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return out


def list_comments(
    client: Client, post_id: str, order: str = "LATEST", after: str | None = None
) -> dict:
    return client.gql(Q_COMMENTS, {"postId": post_id, "order": order, "after": after}, "Q")[
        "comments"
    ]


def latest_replies(client: Client) -> dict:
    return client.gql(Q_LATEST_REPLIES, operation="Q")["viewer"]["user"]


def comment_context(client: Client, comment_id: str) -> dict:
    """一条评论的完整上下文：原帖全文 + 这条评论 + 楼里已有的回复。

    楼中楼回复最容易翻车的地方是答非所问。原来只把 comment.content 塞进
    prompt，模型看不到原帖在讲什么，也不知道楼上已经说过什么，
    于是人问「那家店还开吗」，它回「确实挺好吃的」。
    """
    return (client.gql(Q_COMMENT_CONTEXT, {"id": comment_id}, "Q") or {}).get("comment") or {}


def my_posts(client: Client, user_id: str, after: str | None = None) -> dict:
    return client.gql(Q_MY_POSTS, {"id": user_id, "after": after}, "Q")["user"]["userPosts"]


def my_comments(client: Client, user_id: str, after: str | None = None) -> dict:
    return client.gql(Q_MY_COMMENTS, {"id": user_id, "after": after}, "Q")["user"][
        "userComments"
    ]


def my_subcomments(client: Client, user_id: str, after: str | None = None) -> dict:
    return client.gql(Q_MY_SUBCOMMENTS, {"id": user_id, "after": after}, "Q")["user"][
        "userSubcomments"
    ]


def create_post(
    client: Client,
    content: str,
    school_id: str,
    topic_id: str,
    subtopic_id: str | None = None,
    is_anonymous: bool = False,
    images: list[str] | None = None,
) -> dict:
    return client.gql(
        M_CREATE_POST,
        {
            "content": content,
            "images": images or [],
            "schoolId": school_id,
            "topicId": topic_id,
            "subtopicId": subtopic_id,
            "isAnonymous": is_anonymous,
            "activitiesInfo": [],
        },
        "M",
    )["post"]


def create_comment(
    client: Client, post_id: str, content: str, is_anonymous: bool = False
) -> dict:
    return client.gql(
        M_CREATE_COMMENT,
        {"postId": post_id, "content": content, "images": [], "isAnonymous": is_anonymous},
        "M",
    )["comment"]


def create_subcomment(
    client: Client,
    comment_id: str,
    content: str,
    reply_to_id: str | None = None,
    is_anonymous: bool = False,
) -> dict:
    return client.gql(
        M_CREATE_SUBCOMMENT,
        {
            "commentId": comment_id,
            "content": content,
            "isAnonymous": is_anonymous,
            "replyToId": reply_to_id,
        },
        "M",
    )["subcomment"]


def add_like(client: Client, target_id: str) -> dict:
    return client.gql(M_ADD_LIKE, {"id": target_id}, "M")


def remove_like(client: Client, target_id: str) -> dict:
    return client.gql(M_REMOVE_LIKE, {"id": target_id}, "M")


def add_mark(client: Client, post_id: str) -> dict:
    return client.gql(M_ADD_MARK, {"postId": post_id}, "M")


def delete_post(client: Client, post_id: str) -> Any:
    return client.gql(M_DELETE_POST, {"id": post_id}, "M")


def delete_comment(client: Client, comment_id: str) -> Any:
    return client.gql(M_DELETE_COMMENT, {"id": comment_id}, "M")


def delete_subcomment(client: Client, subcomment_id: str) -> Any:
    return client.gql(M_DELETE_SUBCOMMENT, {"id": subcomment_id}, "M")


def register(client: Client, username: str, password: str) -> dict:
    """用户名密码注册，完全不经过微信。返回 {id, accessToken, refreshToken}。"""
    return client.gql(M_REGISTER, {"u": username, "p": password}, "M")["register"]


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
