import asyncio
import datetime
import json
import time
from datetime import datetime, date
from pprint import pprint
from typing import List, Dict, TypeVar, Type, Tuple

import aiohttp
import feedparser
from aiocron import crontab
from feedparser import FeedParserDict
from langchain.output_parsers import PydanticOutputParser
from metagpt.actions import Action
from metagpt.logs import logger
from metagpt.roles import Role
from metagpt.schema import Message
from metagpt.subscription import SubscriptionRunner
from pydantic import BaseModel, Field
from pytz import BaseTzInfo


class CronTrigger:
    def __init__(self, cron: crontab):
        self.cron = cron
        self.__first_time = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> Message:
        # await self.cron.next()
        await asyncio.sleep(1 if not self.__first_time else 60)
        self.__first_time = True

        return Message(content="triggered by cron")


class GetRSS(Action):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def run(self, url_list: str) -> str:
        # todo: try 3 times?
        articles = await asyncio.gather(*[
            self.get_todays_articles_from_rss(url)
            for url in url_list
        ])
        articles = [a for a_list in articles for a in a_list]  # flat article_list
        summary = [
            {"title": news.title, "summary": news.summary, "link": news.link}
            for news in articles
        ]
        pprint(summary)
        return json.dumps(summary, indent=4)

    async def get_todays_articles_from_rss(self, url) -> List[FeedParserDict]:
        html = await self.craw_html(url)
        feed = feedparser.parse(html)
        todays_articles = [
                              news for news in feed.entries
                              # if self._get_article_date(news) == datetime.today().date()
                          ][:10]
        assert todays_articles != []
        return todays_articles

    def _get_article_date(self, news: FeedParserDict) -> date:
        return datetime.fromtimestamp(time.mktime(news.published_parsed)).date()

    @staticmethod
    async def craw_html(url: str) -> str:
        async with aiohttp.ClientSession() as client:
            async with client.get(url, ssl=False) as response:
                response.raise_for_status()
                return await response.text()


TITLE = str
CONTENT = str
CHAPTER = int


class Article(BaseModel):
    class Directory(BaseModel):
        directory_list: List[TITLE] = Field(..., title="The list of titles of the chapters")

    raw_data: str = Field(..., title="The raw data of the article")
    content: Dict[CHAPTER, Tuple[TITLE, CONTENT]] = (
        Field(..., title="The content of the article as [CHAPTER, (TITLE, CONTENT)]"))

    @property
    def directory(self) -> List[TITLE]:
        return [chapter_content[0] for _, chapter_content in self.content.items()]


T = TypeVar("T", bound=BaseModel)


def parse_output_prompt(answer: str, pydantic_object: Type[T]) -> str:
    PROMPT_TEMPLATE = """
            The answer is: 
            {answer}
            -------------------------
            Thanks for your help. Now I need you to do me a favor to output the result in a specific format.
            Format is given below:
            {format}
            -------------------------
            Please enter your answer below:
            """

    return PROMPT_TEMPLATE.format(answer=answer, format=PydanticOutputParser(
        pydantic_object=pydantic_object
    ).get_format_instructions())


class WriteDirectory(Action):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def run(self, rss_raw: str) -> Article:
        answer = await self._aask(
            f"""Summarize the latest news from the RSS feed:
            ------------------------------------------------
            {rss_raw}
            ------------------------------------------------
            Separate the articles into several chapters base on what they are talking about.
            List the titles of the chapters in order.
            """
        )

        answer_formatted = await self._aask(
            parse_output_prompt(answer, Article.Directory)
        )

        directory = Article.Directory(**json.loads(
            answer_formatted.replace("```", "")[4:]
        ))  # 能不能解析听天由命吧

        return Article(
            raw_data=rss_raw,
            content={
                i + 1: (title, "")
                for i, title in enumerate(directory.directory_list)
            }
        )


class WriteContent(Action):
    def __init__(self, chapter: int, **kwargs):
        super().__init__(**kwargs)
        self.chapter = chapter

    async def run(self, article: Article) -> Article:
        PROMPT_TEMPLATE = """
        {raw_data}
        According to the raw rss feed, I want to write a summary article.
        The Directory of the article is:
        {directory}
        I want to write one of the chapters, the title is:
        {title}
        -------------------------
        Please enter your answer below, Just the article, no other information.
        """

        title = await self._get_title(article)
        content = await self._aask(
            PROMPT_TEMPLATE.format(raw_data=article.raw_data, directory=article.directory, title=title)
        )

        article.content[self.chapter] = (title, content)
        return article

    async def _get_title(self, article):
        return article.content[self.chapter][0]


class OutputContent(Action):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def run(self, article: Article) -> None:
        final_article = """
        # The Summary
        
        """

        for chapter, (title, content) in article.content.items():
            final_article += f"""
            ## {title}
            
            {content}
            
            """

        pprint(final_article)


class RssWatcher(Role):
    url_list: List[str]
    article: Article = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_actions([GetRSS, WriteDirectory])
        self._set_react_mode(react_mode="by_order")

    async def _act(self) -> Message:
        logger.info(f"{self._setting}: to do {self.rc.todo}({self.rc.todo.name})")

        todo = self.rc.todo

        msg = await self._get_last_msg()
        match todo:
            case GetRSS():
                result = await todo.run(self.url_list)
            case WriteDirectory():
                result = await todo.run(msg.content)
                self.article = result
                self._add_action(self.article)
            case _:
                result = await todo.run(self.article)

        msg = Message(content=str(result), role=self.profile, cause_by=type(todo))
        self.rc.memory.add(msg)

        return msg

    def _add_action(self, article: Article):
        actions: List[Action] = [WriteContent(chapter=chapter) for chapter in article.content.keys()]
        actions.append(OutputContent())
        self.set_actions(actions)
        pprint(self.actions)

    async def _get_last_msg(self):
        msg = self.get_memories(k=1)[0]  # find the most k recent messages
        return msg


async def main():
    runner = SubscriptionRunner()

    async def callback(msg):
        print(msg)

    await runner.subscribe(
        role=RssWatcher(url_list=[
            "https://rsshub.mubibai.com/huggingface/daily-papers",
        ]),
        trigger=CronTrigger(cron=crontab("*/5 * * * *", tz=BaseTzInfo("Asia/Shanghai"))),
        callback=callback
    )
    await runner.run()


if __name__ == '__main__':
    asyncio.run(main())
